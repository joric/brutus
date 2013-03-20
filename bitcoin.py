# -*- Mode: Python -*-

# A prototype bitcoin implementation.
#
# Author: Sam Rushing. http://www.nightmare.com/~rushing/
# July 2011 - Mar 2013
#
# because we can have forks/orphans, 
# num->block is 1->N and block->num is 1->1
#

import copy
import hashlib
import random
import struct
import socket
import time
import os
import pickle
import string
import sys

import coro
import coro.read_stream

from hashlib import sha256
from pprint import pprint as pp

import caesure.proto

from caesure.script import parse_script, eval_script, verifying_machine

W = coro.write_stderr

# these are overriden for testnet
BITCOIN_PORT = 8333
BITCOIN_MAGIC = '\xf9\xbe\xb4\xd9'
BLOCKS_PATH = 'blocks.bin'
genesis_block_hash = '000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f'

from caesure.proto import base58_encode, base58_decode

def dhash (s):
    return sha256(sha256(s).digest()).digest()

def rhash (s):
    h1 = hashlib.new ('ripemd160')
    h1.update (sha256(s).digest())
    return h1.digest()

def bcrepr (n):
    return '%d.%08d' % divmod (n, 100000000)

# https://en.bitcoin.it/wiki/Proper_Money_Handling_(JSON-RPC)
def float_to_btc (f):
    return long (round (f * 1e8))

class BadAddress (Exception):
    pass

def key_to_address (s):
    checksum = dhash ('\x00' + s)[:4]
    return '1' + base58_encode (
        int ('0x' + (s + checksum).encode ('hex'), 16)
        )

def address_to_key (s):
    # strip off leading '1'
    s = ('%048x' % base58_decode (s[1:])).decode ('hex')
    hash160, check0 = s[:-4], s[-4:]
    check1 = dhash ('\x00' + hash160)[:4]
    if check0 != check1:
        raise BadAddress (s)
    return hash160

def pkey_to_address (s):
    s = '\x80' + s
    checksum = dhash (s)[:4]
    return base58_encode (
        int ((s + checksum).encode ('hex'), 16)
        )

# for some reason many hashes are reversed, dunno why.  [this may just be block explorer?]
def hexify (s, flip=False):
    if flip:
        return s[::-1].encode ('hex')
    else:
        return s.encode ('hex')

def unhexify (s, flip=False):
    if flip:
        return s.decode ('hex')[::-1]
    else:
        return s.decode ('hex')

def frob_hash (s):
    r = []
    for i in range (0, len (s), 2):
        r.append (s[i:i+2])
    r.reverse()
    return ''.join (r)

class timer:
    def __init__ (self):
        self.start = time.time()
    def end (self):
        return time.time() - self.start

# --------------------------------------------------------------------------------
#        ECDSA
# --------------------------------------------------------------------------------

# pull in one of the ECDSA key implementations.

from ecdsa_ssl import KEY
#from ecdsa_pure import KEY

# --------------------------------------------------------------------------------

OBJ_TX    = 1
OBJ_BLOCK = 2

MAX_BLOCK_SIZE = 1000000
COIN           = 100000000
MAX_MONEY      = 21000000 * COIN

def pack_net_addr ((services, (addr, port))):
    addr = pack_ip_addr (addr)
    port = struct.pack ('!H', port)
    return struct.pack ('<Q', services) + addr + port

def make_nonce():
    return random.randint (0, 1<<64L)

NULL_OUTPOINT = ('\x00' * 32, 4294967295)

class TX (caesure.proto.TX):

    ## def __init__ (self, inputs, outputs, lock_time, raw=None):
    ##     self.inputs = inputs
    ##     self.outputs = outputs
    ##     self.lock_time = lock_time
    ##     self.raw = raw

    def copy (self):
        tx0 = TX()
        tx0.version = self.version
        tx0.lock_time = self.lock_time
        tx0.inputs = self.inputs[:]
        tx0.outputs = self.outputs[:]
        return tx0

    def get_hash (self):
        return dhash (self.render())

    def dump (self):
        print 'hash: %s' % (hexify (dhash (self.render())),)
        print 'inputs: %d' % (len(self.inputs))
        for i in range (len (self.inputs)):
            (outpoint, index), script, sequence = self.inputs[i]
            print '%3d %s:%d %s %d' % (i, hexify(outpoint), index, hexify (script), sequence)
        print '%d outputs' % (len(self.outputs))
        for i in range (len (self.outputs)):
            value, pk_script = self.outputs[i]
            kind, addr = parse_oscript (pk_script)
            if not addr:
                addr = hexify (pk_script)
            print '%3d %s %s %r' % (i, bcrepr (value), kind, addr)
        print 'lock_time:', self.lock_time

    def render (self):
        return self.pack()

    # Hugely Helpful: http://forum.bitcoin.org/index.php?topic=2957.20
    # NOTE: this currently verifies only 'standard' address transactions.
    def get_ecdsa_hash (self, index):
        tx0 = self.copy()
        iscript = tx0.inputs[index][1]
        # build a new version of the input script as an output script
        sig, pubkey = parse_iscript (iscript)
        pubkey_hash = rhash (pubkey)
        new_script = chr(118) + chr (169) + chr (len (pubkey_hash)) + pubkey_hash + chr (136) + chr (172)
        for i in range (len (tx0.inputs)):
            outpoint, script, sequence = tx0.inputs[i]
            if i == index:
                script = new_script
            else:
                script = ''
            tx0.inputs[i] = outpoint, script, sequence
        to_hash = tx0.render() + struct.pack ('<I', 1)
        return dhash (to_hash), sig, pubkey

    def get_ecdsa_hash0 (self, index, sub_script, hash_type):
        tx0 = self.copy()
        for i in range (len (tx0.inputs)):
            outpoint, script, sequence = tx0.inputs[i]
            if i == index:
                script = sub_script
            else:
                script = ''
            tx0.inputs[i] = outpoint, script, sequence
        return tx0.render() + struct.pack ('<I', hash_type)

    def sign (self, key, index):
        hash, _, pubkey = self.get_ecdsa_hash (index)
        assert (key.get_pubkey() == pubkey)
        # tack on the hash type byte.
        sig = key.sign (hash) + '\x01'
        iscript = make_iscript (sig, pubkey)
        op0, _, seq = self.inputs[index]
        self.inputs[index] = op0, iscript, seq
        return sig

    def verify0 (self, index, prev_outscript):
        outpoint, script, sequence = self.inputs[index]
        m = verifying_machine (prev_outscript, self, index)
        eval_script (m, parse_script (script))
        m.clear_alt()
        # should terminate with OP_CHECKSIG or its like
        eval_script (m, parse_script (prev_outscript))

    def verify1 (self, pub_key, sig, vhash):
        k = KEY()
        k.set_pubkey (pub_key)
        return k.verify (vhash, sig)

    def verify (self, index):
        outpoint, script, sequence = self.inputs[index]
        if outpoint == NULL_OUTPOINT:
            # generation is considered verified - I assume by virtue of its hash value?
            return 1
        else:
            hash, sig, pubkey = self.get_ecdsa_hash (index)
            k = KEY()
            k.set_pubkey (pubkey)
            return k.verify (hash, sig)

# both generation and address push two values, so this is good for most things.

# The two numbers pushed by the input script for generation are
# *usually* a compact representation of the current target, and the
# 'extraNonce'.  But there doesn't seem to be any requirement for that;
# the eligius pool uses the string 'Elegius'.

def parse_iscript (s):
    # these tend to be push, push
    s0 = ord (s[0])
    if s0 > 0 and s0 < 76:
        # specifies the size of the first key
        k0 = s[1:1+s0]
        if len(s) == 1+s0:
            return k0, None
        else:
            s1 = ord (s[1+s0])
            if s1 > 0 and s1 < 76:
                k1 = s[2+s0:2+s0+s1]
                return k0, k1
            else:
                return None, None
    else:
        return None, None

def make_iscript (sig, pubkey):
    # XXX assert length limits
    sl = len (sig)
    kl = len (pubkey)
    return chr(sl) + sig + chr(kl) + pubkey

def parse_oscript (s):
    if (ord(s[0]) == 118 and ord(s[1]) == 169 and ord(s[-2]) == 136 and ord(s[-1]) == 172):
        # standard address output: OP_DUP OP_HASH160 <pubKeyHash> OP_EQUALVERIFY OP_CHECKSIG
        size = ord(s[2])
        addr = key_to_address (s[3:size+3])
        assert (size+5 == len(s))
        return 'address', addr
    elif ord(s[0]) == (len(s) - 2) and ord (s[-1]) == 0xac:
        # generation: <pubkey>, OP_CHECKSIG
        return 'pubkey', s[1:-1]
    else:
        return 'unknown', s

def make_oscript (addr):
    # standard tx oscript
    key_hash = address_to_key (addr)
    return chr(118) + chr(169) + chr(len(key_hash)) + key_hash + chr(136) + chr(172)

def read_ip_addr (s):
    r = socket.inet_ntop (socket.AF_INET6, s)
    if r.startswith ('::ffff:'):
        return r[7:]
    else:
        return r

def pack_ip_addr (addr):
    # only v4 right now
    return socket.inet_pton (socket.AF_INET6, '::ffff:%s' % (addr,))

def pack_var_int (n):
    if n < 0xfd:
        return chr(n)
    elif n < 1<<16:
        return '\xfd' + struct.pack ('<H', n)
    elif n < 1<<32:
        return '\xfe' + struct.pack ('<I', n)
    else:
        return '\xff' + struct.pack ('<Q', n)

def pack_var_str (s):
    return pack_var_int (len (s)) + s

def pack_inv (pairs):
    result = [pack_var_int (len(pairs))]
    for objid, hash in pairs:
        result.append (struct.pack ('<I32s', objid, hash))
    return ''.join (result)

class BadBlock (Exception):
    pass

class BLOCK (caesure.proto.BLOCK):

    def make_TX (self):
        return TX()

    def check_bits (self):
        shift  = self.bits >> 24
        target = (self.bits & 0xffffff) * (1 << (8 * (shift - 3)))
        val = int (self.name, 16)
        return val < target

    def get_merkle_hash (self):
        hl = [dhash (t.raw) for t in self.transactions]
        while 1:
            if len(hl) == 1:
                return hl[0]
            if len(hl) % 2 != 0:
                hl.append (hl[-1])
            hl0 = []
            for i in range (0, len (hl), 2):
                hl0.append (dhash (hl[i] + hl[i+1]))
            hl = hl0

    # see https://en.bitcoin.it/wiki/Protocol_rules
    def check_rules (self):
        if not len(self.transactions):
            raise BadBlock ("zero transactions")
        elif not self.check_bits():
            raise BadBlock ("did not achieve target")
        elif (time.time() - self.timestamp) < (-60 * 60 * 2):
            raise BadBlock ("block from the future")
        else:
            for i in range (len (self.transactions)):
                tx = self.transactions[i]
                if i == 0 and (len (tx.inputs) != 1 or tx.inputs[0][0] != NULL_OUTPOINT):
                    raise BadBlock ("first transaction not a generation")
                elif i == 0 and not (2 <= len (tx.inputs[0][1]) <= 100):
                    raise BadBlock ("bad sig_script in generation transaction")
                elif i > 0:
                    for outpoint, sig_script, sequence in tx.inputs:
                        if outpoint == NULL_OUTPOINT:
                            raise BadBlock ("transaction other than the first is a generation")
                for value, _ in tx.outputs:
                    if value > MAX_MONEY:
                        raise BadBlock ("too much money")
                    # XXX not checking SIGOP counts since we don't really implement the script engine.
            # check merkle hash
            if self.merkle_root != self.get_merkle_hash():
                raise BadBlock ("merkle hash doesn't match")
        # XXX more to come...

# --------------------------------------------------------------------------------
# block_db file format: (<8 bytes of size> <block>)+

ZERO_BLOCK = '00' * 32

class block_db:

    def __init__ (self, read_only=False):
        self.read_only = read_only
        self.blocks = {}
        self.prev = {}
        self.next = {}
        self.block_num = {ZERO_BLOCK: -1}
        self.num_block = {}
        self.last_block = 0
        self.build_block_chain()
        self.file = None

    def get_header (self, name):
        path = os.path.join ('blocks', name)
        return open (path).read (80)

    # block can have only one previous block, but may have multiple
    #  next blocks.
    def build_block_chain (self):
        if not os.path.isfile (BLOCKS_PATH):
            open (BLOCKS_PATH, 'wb').write('')
        file = open (BLOCKS_PATH, 'rb')
        print 'reading block headers...'
        file.seek (0)
        i = -1
        name = ZERO_BLOCK
        # first, read all the blocks
        t0 = timer()
        while 1:
            pos = file.tell()
            size = file.read (8)
            if not size:
                break
            else:
                size, = struct.unpack ('<Q', size)
                header = file.read (80)
                (version, prev_block, merkle_root,
                 timestamp, bits, nonce) = caesure.proto.unpack_block_header (header)
                # skip the rest of the block
                file.seek (size-80, 1)
                prev_block = hexify (prev_block, True)
                name = hexify (dhash (header), True)
                bn = 1 + self.block_num[prev_block]
                self.prev[name] = prev_block
                self.next.setdefault (prev_block, set()).add (name)
                self.block_num[name] = bn
                self.num_block.setdefault (bn, set()).add (name)
                self.blocks[name] = pos
                self.last_block = max (self.last_block, bn)
        print 'last block (%d): %r' % (self.last_block, self.num_block[self.last_block])
        file.close()
        print '%.02f secs to load block chain' % (t0.end())
        self.read_only_file = open (BLOCKS_PATH, 'rb')

    def open_for_append (self):
        # reopen in append mode
        self.file = open (BLOCKS_PATH, 'ab')

    def get_block (self, name):
        pos =  self.blocks[name]
        self.read_only_file.seek (pos)
        size = self.read_only_file.read (8)
        size, = struct.unpack ('<Q', size)
        return self.read_only_file.read (size)

    def __getitem__ (self, name):
        b = BLOCK()
        b.unpack (self.get_block (name))
        return b

    def __len__ (self):
        return len (self.blocks)

    def by_num (self, num):
        # fetch *one* of the set, beware all callers of this
        return self[list(self.num_block[num])[0]]

    def add (self, name, block):
        if self.blocks.has_key (name):
            print 'ignoring block we already have:', name
        elif not self.block_num.has_key (block.prev_block) and block.prev_block != ZERO_BLOCK:
            # if we don't have the previous block, there's no
            #  point in remembering it at all.  toss it.
            pass
        else:
            self.write_block (name, block)

    def write_block (self, name, block):
        if self.file is None:
            self.open_for_append()
        size = len (block.raw)
        pos = self.file.tell()
        self.file.write (struct.pack ('<Q', size))
        self.file.write (block.raw)
        self.file.flush()
        self.prev[name] = block.prev_block
        self.next.setdefault (block.prev_block, set()).add (name)
        self.blocks[name] = pos
        if block.prev_block == ZERO_BLOCK:
            i = -1
        else:
            i = self.block_num[block.prev_block]
        self.block_num[name] = i+1
        self.num_block.setdefault (i+1, set()).add (name)
        self.last_block = i+1

    def has_key (self, name):
        return self.prev.has_key (name)

    # see https://en.bitcoin.it/wiki/Satoshi_Client_Block_Exchange
    # "The getblocks message contains multiple block hashes that the
    #  requesting node already possesses, in order to help the remote
    #  note find the latest common block between the nodes. The list of
    #  hashes starts with the latest block and goes back ten and then
    #  doubles in an exponential progression until the genesis block is
    #  reached."

    def set_for_getblocks (self):
        n = self.last_block
        result = []
        i = 0
        step = 1
        while n > 0:
            name = list(self.num_block[n])[0]
            result.append (unhexify (name, flip=True))
            n -= step
            i += 1
            if i >= 10:
                step *= 2
        return result
    
# --------------------------------------------------------------------------------
#                               protocol
# --------------------------------------------------------------------------------

class BadState (Exception):
    pass

# we need a way to do command/response on the connection, and we need to do something
#   to handle asynchronous commands (like continual inv) as well.  So I think we
#   need a mechanism to identify which things are waiting for responses (hopefully
#   the protocol makes this easy?).
#
# how to distinguish the answer we expected from an async one?  can we do it at the
#   protocol level?
#   addr: in response to getaddr: ok this one we can consider to not be command/response?
#   inv: this one I think we can figure out by checking the answer?

# commands are getblocks, getheaders.  we've never used getheaders, so the only
#   ACTUAL command is getblocks.  hmmm.. maybe that's why I did this async?
#
# so we can make a fifo/cv/etc for managing the getblocks/inv/etc 

# messages:            expect-async?   command    response   response-to
# 3.1 version                *            X           
# 3.2 verack                                          X        version
# 3.3 addr                   X                        X        getaddr
# 3.4 inv                    X                        X        getblocks
# 3.5 getdata                                         X        inv
# 3.6 notfound                                        X        getdata
# 3.7 getblocks                           X
# 3.8 getheaders                          X
# 3.9 tx                                              X        getdata
# 3.10 block                                          X        getdata
# 3.11 headers                                        X        getheaders
# 3.12 getaddr                            X

# for IP transactions (used?)
# 3.13 checkorder
# 3.14 submitorder
# 3.15 reply

# 3.16 ping                  X             
# 3.18 alert                 X


# ignore for now?
# 3.17 filterload, filteradd, filterclear, merkleblock

# the only totally unsolicited packets we expect are <addr>, <inv>

class dispatcher:
    def __init__ (self):
        self.known = []
        self.requested = set()
        self.ready = {}
        self.target = 0
        if not the_block_db:
            self.known.append (genesis_block_hash)

    def notify_height (self, height):
        if height > self.target:
            self.target = height
            c = get_random_connection()
            W ('sending getblocks() target=%d\n' % (self.target,))
            c.getblocks()

    def add_inv (self, objid, name):
        if objid == OBJ_BLOCK:
            self.add_to_known (name)
        elif objid == OBJ_TX:
            pass
        else:
            W ('*** strange <inv> of type %r %r\n' % (objid, name))

    def add_block (self, payload):
        db = the_block_db
        b = BLOCK()
        b.unpack (payload)
        self.ready[b.prev_block] = b
        if b.name in self.requested:
            self.requested.remove (b.name)
        # we may have several blocks waiting to be chained
        #  in by the arrival of a missing link...
        while 1:
            if db.has_key (b.prev_block) or (b.prev_block == ZERO_BLOCK):
                del self.ready[b.prev_block]
                self.block_to_db (b.name, b)
                if self.ready.has_key (b.name):
                    b = self.ready[b.name]
                else:
                    break
            else:
                break
        if not len(self.requested):
            c = get_random_connection()
            c.getblocks()
        
    def kick_known (self):
        chunk = min (self.target - the_block_db.last_block, 100)
        if len (self.known) >= chunk:
            c = get_random_connection()
            chunk, self.known = self.known[:100], self.known[100:]
            c.getdata ([(OBJ_BLOCK, name) for name in chunk])
            self.requested.update (chunk)

    def add_to_known (self, name):
        self.known.append (name)
        self.kick_known()

    def block_to_db (self, name, b):
        try:
            b.check_rules()
        except BadState as reason:
            W ('*** bad block: %s %r' % (name, reason))
        else:
            the_block_db.add (name, b)

class base_connection:

    # protocol was changed to reflect *protocol* version, not bitcoin-client-version
    version = 60002 # trying to update protocol from 60001 mar 2013

    def __init__ (self, addr='127.0.0.1', port=BITCOIN_PORT):
        self.addr = addr
        self.port = port
        self.nonce = make_nonce()
        self.conn = coro.tcp_sock()
        self.packet_count = 0
        self.stream = coro.read_stream.sock_stream (self.conn)

    def connect (self):
        self.conn.connect ((self.addr, self.port))

    def send_packet (self, command, payload):
        lc = len(command)
        assert (lc < 12)
        cmd = command + ('\x00' * (12 - lc))
        h = dhash (payload)
        checksum, = struct.unpack ('<I', h[:4])
        packet = struct.pack (
            '<4s12sII',
            BITCOIN_MAGIC,
            cmd,
            len(payload),
            checksum
            ) + payload
        self.conn.send (packet)
        W ('=> %s\n' % (command,))

    def send_version (self):
        data = struct.pack ('<IQQ', self.version, 1, int(time.time()))
        data += pack_net_addr ((1, (self.addr, self.port)))
        data += pack_net_addr ((1, (my_addr, BITCOIN_PORT)))
        data += struct.pack ('<Q', self.nonce)
        data += pack_var_str ('/caesure:20130306/')
        start_height = the_block_db.last_block
        if start_height < 0:
            start_height = 0
        # ignore bip37 for now - leave True
        data += struct.pack ('<IB', start_height, 1)
        self.send_packet ('version', data)

    def gen_packets (self):
        while 1:
            data = self.stream.read_exact (24)
            if not data:
                W ('connection closed.\n')
                break
            magic, command, length, checksum = struct.unpack ('<I12sII', data)
            command = command.strip ('\x00')
            W ('[%s]' % (command,))
            self.packet_count += 1
            self.header = magic, command, length
            # XXX verify checksum
            if length:
                payload = self.stream.read_exact (length)
            else:
                payload = ''
            yield (command, payload)

    def getblocks (self):
        hashes = the_block_db.set_for_getblocks()
        hashes.append ('\x00' * 32)
        payload = ''.join ([
            struct.pack ('<I', self.version),
            pack_var_int (len(hashes)-1), # count does not include hash_stop
            ] + hashes
            )
        self.send_packet ('getblocks', payload)

    def getdata (self, what):
        "request (TX|BLOCK)+ from the other side"
        payload = [pack_var_int (len(what))]
        for kind, name in what:
            # decode hash
            h = unhexify (name, flip=True)
            payload.append (struct.pack ('<I32s', kind, h))
        self.send_packet ('getdata', ''.join (payload))

class connection (base_connection):

    def __init__ (self, addr='127.0.0.1', port=BITCOIN_PORT):
        base_connection.__init__ (self, addr, port)
        coro.spawn (self.go)

    def go (self):
        self.connect()
        try:
            the_connection_list.append (self)
            self.send_version()
            for command, payload in self.gen_packets():
                self.do_command (command, payload)
        finally:
            the_connection_list.remove (self)
            self.conn.close()

    def check_command_name (self, command):
        for ch in command:
            if ch not in string.letters:
                return False
        return True

    def do_command (self, cmd, data):
        if self.check_command_name (cmd):
            try:
                method = getattr (self, 'cmd_%s' % cmd,)
            except AttributeError:
                W ('no support for "%s" command\n' % (cmd,))
            else:
                try:
                    method (data)
                except:
                    W ('caesure error: %r\n' % (coro.compact_traceback(),))
                    W ('     ********** problem processing %r command\n' % (cmd,))
        else:
            W ('bad command: "%r", ignoring\n' % (cmd,))

    max_pending = 50

    def cmd_version (self, data):
        self.other_version = caesure.proto.unpack_version (data)
        self.send_packet ('verack', '')
        the_dispatcher.notify_height (self.other_version.start_height)

    def cmd_verack (self, data):
        pass

    def cmd_addr (self, data):
        addr = caesure.proto.unpack_addr (data)
        print addr

    def cmd_inv (self, data):
        pairs = caesure.proto.unpack_inv (data)
        for objid, name in pairs:
            the_dispatcher.add_inv (objid, hexify (name, True))

    def cmd_getdata (self, data):
        return caesure.proto.unpack_getdata (data)

    def cmd_tx (self, data):
        return caesure.proto.make_tx (data)

    def cmd_block (self, data):
        the_dispatcher.add_block (data)

    def cmd_ping (self, data):
        # supposed to do a pong?
        W ('ping: data=%r\n' % (data,))

    def cmd_alert (self, data):
        payload, signature = caesure.proto.unpack_alert (data)
        # XXX verify signature
        W ('alert: sig=%r payload=%r\n' % (signature, payload,))

the_block_db = None
the_connection_list = []

def get_random_connection():
    return random.choice (the_connection_list)

# Mar 2013 fetched from https://github.com/bitcoin/bitcoin/blob/master/src/net.cpp
dns_seeds = [
    "bitseed.xf2.org",
    "dnsseed.bluematt.me",
    "seed.bitcoin.sipa.be",
    "dnsseed.bitcoin.dashjr.org",
    ]

def dns_seed():
    print 'fetching DNS seed addresses...'
    addrs = set()
    for name in dns_seeds:
        for info in socket.getaddrinfo (name, 8333):
            family, type, proto, _, addr = info
            if family == socket.AF_INET and type == socket.SOCK_STREAM and proto == socket.IPPROTO_TCP:
                addrs.add (addr[0])
    print '...done.'
    return addrs

def do_sample_verify():
    db = the_block_db
    b = db.by_num (226670)
    tx = b.transactions[-1]
    tx.verify0 (0, 'v\xa9\x14\x06\xf1\xb6py\x1f\x92V\xbf\xfc\x89\x8fGBq\xc2/K\xb9I\x88\xac')
    tx.verify0 (1, 'v\xa9\x14\r,\x81^:\xca\x96Wei\xaab\xd5\xfd\x06\xad\x11\x16\xa5\xd7\x88\xac')

if __name__ == '__main__':
    if '-t' in sys.argv:
        BITCOIN_PORT = 18333
        BITCOIN_MAGIC = '\xfa\xbf\xb5\xda'
        BLOCKS_PATH = 'blocks.testnet.bin'
        genesis_block_hash = '00000007199508e34a9ff81e6ec0c477a4cccff2a4767a8eee39c11db367b008'

    # mount the block database
    the_block_db = block_db()
    the_dispatcher = dispatcher()
    network = False

    # client mode
    if '-c' in sys.argv:
        i = sys.argv.index ('-c')
        [my_addr, other_addr] = sys.argv[i+1:i+3]
        bc = connection (other_addr)
        network = True

    # network mode
    if '-n' in sys.argv:
        i = sys.argv.index ('-n')
        my_addr = sys.argv[i+1]
        addrs = dns_seed()
        for addr in addrs:
            connection (addr)
        network = True

    do_monitor = '-m' in sys.argv
    do_admin   = '-a' in sys.argv

    if network:
        if do_monitor:
            import coro.backdoor
            coro.spawn (coro.backdoor.serve, unix_path='/tmp/caesure.bd')
        if do_admin:
            import coro.http
            import webadmin
            import zlib
            h = coro.http.server()
            coro.spawn (h.start, (('127.0.0.1', 8380)))
            h.push_handler (webadmin.handler())
            h.push_handler (coro.http.handlers.coro_status_handler())
            h.push_handler (coro.http.handlers.favicon_handler (zlib.compress (webadmin.favicon)))
        coro.event_loop()
    else:
        # database browsing mode
        db = the_block_db # alias
