#!/usr/bin/env python3
#
# fix_jtagd.py - fix two USB Blaster bugs in Intel/Altera Quartus jtagd for Linux.
#
# This script patches two annoying bugs in jtagd that cause programming issues:
#
# 1) An uninitialized variable that sometimes causes corrupted USB traffic to be
#    sent to an FT245-based USB Blaster if you replug it multiple times while
#    jtagd is already running.
#
# 2) A 30-second delay the first time you try to use an FT245-based USB Blaster,
#    caused by a specific initialization sequence that results in the FT245
#    discarding data for 250 ms.

import argparse
import io
import os
import re
import shutil
import struct
import sys

from elftools.common.exceptions import ELFError
from elftools.elf.elffile import ELFFile

# Bug 1: The JTAG_SERVER_USB_BLASTER constructor zeroes three 32-bit members
# but skips the one between the second and third:
#
#   movl $0, base+0x0(%rbx)
#   movl $0, base+0x4(%rbx)
#                              <- base+0x8 is never initialized
#   movl $0, base+0xc(%rbx)
#
# The uninitialized variable, if it happens to be nonzero, causes commands
# only meant for newer USB Blasters to be sent to older FT245-based models,
# which corrupts the communication.
#
# This patch works by replacing the three instructions listed above with
# two 64-bit stores that cover all four members, plus a NOP to keep everything
# the same size as it used to be.
#
# base is 0x388 in 18.1 through 24.1. 25.1 added a member that moved it to 0x38c.
CTOR_SYMBOL = re.compile(r'_ZN23JTAG_SERVER_USB_BLASTERC[12]E')
CTOR_BASES = [0x388, 0x38c]


def movl_zero(offset):
    """movl $0, offset(%rbx)"""
    return b'\xc7\x83' + struct.pack('<I', offset) + bytes(4)


def movq_zero(offset):
    """movq $0, offset(%rbx)"""
    return b'\x48\xc7\x83' + struct.pack('<I', offset) + bytes(4)


NOP8 = bytes.fromhex('0f1f840000000000')

# Bug 2: do_start_hardware sends four vendor requests to the FTDI chip, each
# set up like this before calling control_msg:
#
#   b9 xx 00 00 00 --> mov $wValue, %ecx
#   ba xx 00 00 00 --> mov $bRequest, %edx
#   be 40 00 00 00 --> mov $0x40, %esi
#
# They run in the order: reset, purge tx, purge rx, set latency timer. Setting
# the latency timer causes the chip to discard data for 250 ms, which is bad
# because jtagd immediately starts trying to talk to it. Fix it by shifting
# the requests around to set the latency timer first, which seems to work
# around this bizarre behavior.
START_HARDWARE_SYMBOL = '_ZN23JTAG_SERVER_USB_BLASTER17do_start_hardwareEP17JTAG_SERVER_VOICE'
RECEIVE_STREAM_SYMBOL = '_ZN23JTAG_SERVER_USB_BLASTER14receive_streamE'
RECEIVE_TIMEOUT_30S = struct.pack('<I', 30000)
FTDI_REQUEST = re.compile(rb'\xb9(.)\x00\x00\x00\xba(.)\x00\x00\x00\xbe\x40\x00\x00\x00', re.DOTALL)

# (wValue, bRequest)
RESET = (0, 0)
PURGE_RX = (1, 0)
PURGE_TX = (2, 0)
LATENCY_1MS = (1, 9)

FTDI_REORDER = {
    RESET: LATENCY_1MS,
    PURGE_TX: RESET,
    PURGE_RX: PURGE_TX,
    LATENCY_1MS: PURGE_RX,
}

# The order the requests appear in the code, which isn't always
# the same as the order they execute in.
FTDI_LAYOUTS = [
    [RESET, PURGE_TX, PURGE_RX, LATENCY_1MS],   # 13.x, 23.1 and later
    [RESET, PURGE_RX, LATENCY_1MS, PURGE_TX],   # 18.1 through 21.1
]


def ftdi_request(wvalue, brequest):
    """mov $wValue, %ecx; mov $bRequest, %edx"""
    return b'\xb9' + struct.pack('<I', wvalue) + b'\xba' + struct.pack('<I', brequest)


class PatchError(Exception):
    pass


def find_functions(data):
    """Return {symbol name: (file offset, size)} for every function in the ELF file."""
    try:
        elf = ELFFile(io.BytesIO(data))
    except ELFError:
        elf = None
    if elf is None or elf.get_machine_arch() != 'x64':
        raise PatchError('not an x86-64 ELF file (use linux64/jtagd, not the bin/jtagd wrapper)')
    symtab = elf.get_section_by_name('.symtab')
    if symtab is None:
        raise PatchError('no symbol table')

    functions = {}
    for sym in symtab.iter_symbols():
        if sym['st_info']['type'] == 'STT_FUNC' and sym['st_value']:
            offset = next(elf.address_offsets(sym['st_value']))
            functions[sym.name] = (offset, sym['st_size'])
    return functions


def find_ctor_patch(data, functions):
    """Bug 1: returns (status, patches)"""
    ctors = {loc for name, loc in functions.items() if CTOR_SYMBOL.match(name)}
    if len(ctors) != 1:
        raise PatchError(f'expected one USB Blaster constructor, found {len(ctors)}')
    ctor_start, ctor_size = ctors.pop()
    ctor = data[ctor_start:ctor_start + ctor_size]

    for base in CTOR_BASES:
        unpatched = movl_zero(base) + movl_zero(base + 4) + movl_zero(base + 12)
        patched = movq_zero(base) + movq_zero(base + 8) + NOP8
        if patched in ctor:
            return 'already patched', []
        if unpatched in ctor:
            break
    else:
        return 'not affected (or unrecognized version)', []

    # If the constructor touches the skipped member anywhere else, this is a
    # build we don't know about. Don't mess with it.
    if struct.pack('<I', base + 8) in ctor:
        return f'constructor already uses member {base + 8:#x}, leaving it alone', []

    return 'needs patching', [(ctor_start + ctor.index(unpatched), unpatched, patched)]


def find_ftdi_patch(data, functions):
    """Bug 2: returns (status, patches)"""
    # Older versions (13.x) have the same FTDI init sequence, but they don't
    # wait 30 seconds for the lost reply, so they don't need fixing.
    recv = [loc for name, loc in functions.items() if name.startswith(RECEIVE_STREAM_SYMBOL)]
    if len(recv) != 1:
        raise PatchError(f'expected one receive_stream, found {len(recv)}')
    start, size = recv[0]
    if RECEIVE_TIMEOUT_30S not in data[start:start + size]:
        return 'not affected (no 30 second timeout)', []

    if START_HARDWARE_SYMBOL not in functions:
        raise PatchError('do_start_hardware not found')
    start, size = functions[START_HARDWARE_SYMBOL]
    matches = list(FTDI_REQUEST.finditer(data, start, start + size))
    requests = [(m[1][0], m[2][0]) for m in matches]

    for layout in FTDI_LAYOUTS:
        if requests == [FTDI_REORDER[r] for r in layout]:
            return 'already patched', []
        if requests == layout:
            break
    else:
        return f'unrecognized FTDI requests {requests}, leaving them alone', []

    patches = []
    for m, request in zip(matches, requests):
        patches.append((m.start(), ftdi_request(*request), ftdi_request(*FTDI_REORDER[request])))
    return 'needs patching', patches


def find_patches(data):
    """Figure out what needs to change in a jtagd binary.

    Returns (statuses, patches), where statuses is a list of lines to print
    and patches is a list of (offset, old bytes, new bytes).
    """
    functions = find_functions(data)
    ctor_status, ctor_patches = find_ctor_patch(data, functions)
    ftdi_status, ftdi_patches = find_ftdi_patch(data, functions)
    statuses = [f'uninitialized variable: {ctor_status}',
                f'30 second delay: {ftdi_status}']
    return statuses, ctor_patches + ftdi_patches


def write_file(path, data, mode_from):
    # Write to a temporary file and rename it into place, so we never leave a
    # half-written jtagd behind
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    shutil.copymode(mode_from, tmp)
    os.replace(tmp, path)


def check(path):
    with open(path, 'rb') as f:
        statuses, _ = find_patches(f.read())
    print(f'{path}:')
    for status in statuses:
        print(f'  {status}')


def apply(path, output=None):
    with open(path, 'rb') as f:
        data = bytearray(f.read())

    statuses, patches = find_patches(data)
    print(f'{path}:')
    for status in statuses:
        print(f'  {status}')
    if not patches:
        return

    for offset, old, new in patches:
        assert data[offset:offset + len(old)] == old
        data[offset:offset + len(new)] = new

    if output is None:
        output = path
    if os.path.exists(output) and os.path.samefile(output, path):
        backup = path + '.orig'
        if not os.path.exists(backup):
            shutil.copy2(path, backup)
            print(f'  saved original to {backup}')

    write_file(output, data, mode_from=path)
    print(f'  patched {len(patches)} locations, wrote {output}')


def restore(path):
    backup = path + '.orig'
    if not os.path.exists(backup):
        raise PatchError(f'no backup found at {backup}')
    with open(backup, 'rb') as f:
        write_file(path, f.read(), mode_from=backup)
    print(f'{path}: restored from {backup}')


def main():
    parser = argparse.ArgumentParser(
        description='Fix two USB Blaster bugs in Quartus jtagd.',
        epilog='Point this at linux64/jtagd, not the bin/jtagd wrapper script. '
               'Stop jtagd first (killall jtagd) so the fixed version gets loaded.')
    parser.add_argument('command', choices=['check', 'apply', 'restore'],
                        help='check: report status only. apply: back up to jtagd.orig and patch. '
                             'restore: put jtagd.orig back.')
    parser.add_argument('jtagd', nargs='+', help='path to linux64/jtagd')
    parser.add_argument('-o', '--output',
                        help='with apply: write the patched copy here instead of modifying jtagd')
    args = parser.parse_args()

    if args.output and (args.command != 'apply' or len(args.jtagd) != 1):
        parser.error('--output only works with apply and a single jtagd')

    failed = False
    for path in args.jtagd:
        try:
            if args.command == 'check':
                check(path)
            elif args.command == 'apply':
                apply(path, args.output)
            else:
                restore(path)
        except (PatchError, ELFError, OSError) as e:
            print(f'{path}: error: {e}')
            failed = True

    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
