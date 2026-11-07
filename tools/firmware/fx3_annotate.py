#!/usr/bin/env python3
"""fx3_annotate.py -- regenerate the Einstar FX3 function table from the Ghidra corpus.

Inputs (all local, no network):
  analysis/firmware/EinScan10_EN.elf          ELF32-ARM with real load addresses (built by fx3img.py)
  analysis/ghidra/FX3_FW/clean/*.c            one decompiled function per file, named by entry address

Outputs:
  analysis/firmware/fx3_symbols.csv           address,name,role,confidence  (Ghidra-reimportable)
  analysis/firmware/callgraph.json            caller->callee edges + annotations
  stdout / --md FILE                          markdown function table

Method (all evidence is address-anchored):
  * function extents come from the corpus headers (`range:`), which Ghidra derived from its analysis;
  * string references are recovered from ARM literal pools: every 4-byte word inside a function's
    body (and the pool that follows it) whose value equals the address of a NUL-terminated string
    in the loaded image is a proven reference -- the decompiler prints the literal inline, so the
    text alone would be ambiguous when a string is duplicated;
  * callee edges come from `FUN_xxxxxxxx(` tokens in the decompiled body, cross-checked against the
    known function start set (dangling targets are reported separately);
  * 'role' is a short human annotation held in ROLES below; every entry there is *inferred* by the
    analyst from strings/constants/callees, never taken from a symbol table (the image is stripped).

Usage:
  python3 tools/firmware/fx3_annotate.py                     # table to stdout
  python3 tools/firmware/fx3_annotate.py --md docs/firmware/fx3_table.md
  python3 tools/firmware/fx3_annotate.py --csv analysis/firmware/fx3_symbols.csv \
                                          --json analysis/firmware/callgraph.json
"""
import argparse, array, bisect, collections, json, os, re, struct, sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLEAN = os.path.join(ROOT, 'analysis/ghidra/FX3_FW/clean')
ELF = os.path.join(ROOT, 'analysis/firmware/EinScan10_EN.elf')

# ----------------------------------------------------------------------------- ELF image
def load_segments(path):
    d = open(path, 'rb').read()
    if d[:4] != b'\x7fELF':
        sys.exit('not an ELF: %s' % path)
    (_, _, _, entry, phoff, _, _, _, phentsize, phnum, _, _, _) = struct.unpack('<HHIIIIIHHHHHH', d[16:52])
    segs = []
    for i in range(phnum):
        o = phoff + i * phentsize
        p_type, p_off, p_va, _, p_filesz, _, _, _ = struct.unpack('<IIIIIIII', d[o:o + 32])
        if p_type == 1:
            segs.append((p_va, d[p_off:p_off + p_filesz]))
    return segs, entry

SEGS, ENTRY = load_segments(ELF)

def rd(a, n):
    for va, b in SEGS:
        if va <= a < va + len(b):
            return b[a - va:a - va + n]
    return b''

def u32(a):
    b = rd(a, 4)
    return struct.unpack('<I', b)[0] if len(b) == 4 else 0xFFFFFFFF

WORDS = {}
for _va, _b in SEGS:
    _n = len(_b) // 4
    _arr = array.array('I'); _arr.frombytes(_b[:_n * 4])
    WORDS[_va] = _arr

def iter_words(lo, hi):
    for va, arr in WORDS.items():
        s, e = max(lo, va), min(hi, va + len(arr) * 4)
        if s >= e:
            continue
        s = va + ((s - va + 3) // 4) * 4
        for i in range((s - va) // 4, (e - va) // 4):
            yield va + i * 4, arr[i]

# every address inside a printable run that still has >=4 chars before the NUL is a valid string start
STR_ANY = {}
for va, b in SEGS:
    i, n = 0, len(b)
    while i < n:
        j = i
        while j < n and b[j] != 0 and 32 <= b[j] < 127:
            j += 1
        if j - i >= 4:
            for k in range(0, j - i - 3):
                STR_ANY[va + i + k] = b[i + k:j].decode()
        i = j + 1 if j > i else i + 1

# ----------------------------------------------------------------------------- corpus
HDR = re.compile(r'^// ([\w ]+): (.*)$')
CALL = re.compile(r'\b(FUN_[0-9a-f]{6,8})\s*\(')
DAT = re.compile(r'\bDAT_([0-9a-f]{6,8})\b')

def load_funcs():
    funcs = {}
    for fn in sorted(os.listdir(CLEAN)):
        if not fn.endswith('.c'):
            continue
        txt = open(os.path.join(CLEAN, fn), encoding='utf-8', errors='replace').read()
        meta, body, in_body = {}, [], False
        for line in txt.splitlines():
            if not in_body:
                m = HDR.match(line)
                if m:
                    meta[m.group(1)] = m.group(2).strip(); continue
                if line.startswith('//') or not line.strip():
                    continue
                in_body = True
            body.append(line)
        addr = int(meta['address'], 16)
        m = re.match(r'(0x[0-9a-f]+)-(0x[0-9a-f]+) inclusive', meta.get('range', ''))
        lo, hi = (int(m.group(1), 16), int(m.group(2), 16)) if m else (addr, addr)
        src = '\n'.join(body)
        funcs[addr] = dict(addr=addr, name=meta.get('name', ''), lo=lo, hi=hi,
                           size=hi - lo + 1, src=src,
                           calls=sorted({int(x[4:], 16) for x in CALL.findall(src)}),
                           dats=sorted({int(x, 16) for x in DAT.findall(src)}))
    return funcs

FUNCS = load_funcs()
STARTS = sorted(FUNCS)

def nearest_func(a):
    """owner of a literal-pool word: the function whose body contains a, else the one just before"""
    i = bisect.bisect_right(STARTS, a) - 1
    if i < 0:
        return None
    s = STARTS[i]
    if s <= a <= FUNCS[s]['hi']:
        return s
    nxt = STARTS[i + 1] if i + 1 < len(STARTS) else 0x400354d0
    if a < nxt and a - FUNCS[s]['hi'] <= 0x300:
        return s
    return None

def string_refs():
    refs = collections.defaultdict(list)
    for a, w in iter_words(0x100, 0x400354d0):
        if w in STR_ANY:
            o = nearest_func(a)
            if o is not None:
                refs[o].append((a, w, STR_ANY[w]))
    return refs

STRREFS = string_refs()

# Largest 16-bit / 32-bit immediates appearing as constants in a body (cheap "constants" column).
IMM = re.compile(r'\b(0x[0-9a-fA-F]{3,8})\b')
def constants(src):
    vals = {int(x, 16) for x in IMM.findall(src)}
    keep = [v for v in vals if v >= 0x100 and not (0x40000000 <= v < 0x40040000)]
    return sorted(keep)

# ----------------------------------------------------------------------------- annotations
# Analyst-inferred roles.  'confidence' is one of: proven / high / medium / low.
ROLES = {
    0x00000100: ('exception_stub', 'high', 'ITCM stub: push/mov r0,#imm/bl/pop; no ARM vector table in this image'),
    0x00000118: ('exception_stub', 'high', 'ITCM stub (same shape as 0x100)'),
    0x00000130: ('exception_stub', 'high', 'ITCM stub (same shape as 0x100)'),
    0x00000148: ('hook_dispatch', 'medium', 'reads [0x40032DDC+0x10] and tail-branches through it when non-zero'),
    0x00000178: ('exception_stub', 'medium', 'ITCM stub calling 0x2618/0x2620'),
    0x00000194: ('early_init', 'medium', 'pokes 0x40032DDC and a 0xFFFFF0xx system register'),
    0x40003000: ('c_runtime_startup', 'high', 'zero .bss 0x40030640..0x400354D0 then branch to __rt_entry'),
    0x40003060: ('semaphore_take', 'high', 'FUN_40023788(&DAT_400306A4,-1); guards flash writes'),
    0x4000307C: ('semaphore_give', 'high', 'FUN_40023810(&DAT_400306A4)'),
    0x4000310C: ('build_version_string', 'proven', '"EinScan10_01"+"SC130"+"_FX3_V"+"2.10"+"_FPGA_V" literal pool 0x40003354'),
    0x400033E0: ('spi_flash_init_helper', 'low', 'called once from the FPGA boot thread'),
    0x40003510: ('gpif_load_and_start', 'proven', 'CyU3PGpifLoad / CyU3PGpifSMStart error strings'),
    0x400035C0: ('usb_ep_and_dma_config', 'proven', '5 endpoints, 5 DMA channels, sockIds 0x3F00/0x3F01'),
    0x40003C24: ('usb_stop_disconnect', 'proven', '"STOP!!!!" + CyU3PSetEpConfig'),
    0x40003E44: ('usb_reconfigure', 'medium', 'calls FUN_400035C0; invoked on status bit 17'),
    0x40003E88: ('usb_event_callback', 'proven', 'CyU3PUsbRegisterEventCallback target; 13 event strings'),
    0x40004180: ('ep_event_callback', 'proven', '"EP Event CB epNum : %d evtype : %d"; sets DAT_40030681'),
    0x40004210: ('usb_setup_request_handler', 'proven', '32 request strings; dispatch on bmRequestType'),
    0x40004BEC: ('usb_stack_init', 'proven', 'CyU3PUsbStart + 9 x CyU3PUsbSetDesc'),
    0x400050CC: ('thread_fpga_boot', 'proven', '"20:FPGA Boot Thread"'),
    0x400051C8: ('thread_com', 'proven', '"21:comThread"; EP2 loop'),
    0x40005424: ('thread_emc_wdg', 'proven', '"21:EMC_WDG_Thread"; reboot supervisor'),
    0x400055E8: ('thread_control', 'proven', '"21:ControlThread"; EP1 loop'),
    0x400057B0: ('create_app_threads', 'proven', '4 x CyU3PThreadCreate with the 20:/21: name strings'),
    0x40005AEC: ('i2c_read_adt7420', 'proven', '"Failed to ReadI2C_ADT7420", slave 0x90/0x91'),
    0x40005BAC: ('fpga_reg_write_i2c', 'proven', '"write data To FPGA Failed %d", slave 0x20'),
    0x40005C50: ('fpga_reg_read_i2c', 'proven', 'preamble {0x20,reg}, 4-byte big-endian result'),
    0x40005DF8: ('sensor_reg_write_i2c', 'proven', '"Reg write: 0x%x", slave 0x64, 16-bit reg'),
    0x400060A8: ('set_sc132_gain_linear', 'proven', 'SetSc132GainLinearMode GPIO_LVDS_CAM1/2/4'),
    0x400066B0: ('sc130gs_write_table', 'high', '"sc130gs_write_table write seneor_register addr:0x%x val:0x%x"'),
    0x400067C0: ('sc130gs_write_table_per_camera', 'proven', 'GPIO_LVDS_CAM1/CAM2/CAM4 error strings'),
    0x40006990: ('thread_stack_alloc', 'high', 'byte-pool allocate, size rounded up to 4'),
    0x40006C68: ('memset', 'high', 'called as memset(ptr,0,n)'),
    0x40006D68: ('memcpy', 'high', 'called as memcpy(dst,src,n)'),
    0x400079F0: ('spi_cs_wrapper', 'high', 'FUN_4001145C(level) when dev==0'),
    0x40007B3C: ('spi_wren_and_wait', 'proven', 'cmd 0x06 + 0x05 poll; 3 SPI error strings'),
    0x40007C90: ('spi_sector_erase', 'proven', 'cmd 0x20, address = sector<<12'),
    0x40007F64: ('spi_page_program_or_read', 'proven', 'cmd 0x02/0x03, 256-byte pages, addr arg = addr>>8'),
    0x40008218: ('fpga_bitstream_load', 'high', '0x180 x 0x1000 from GetFpgaLoadAddr(), GPIO 0x33'),
    0x400083C4: ('spi_write_status_reg', 'high', 'cmd 0x01 + 2-byte SR'),
    0x40008478: ('spi_read_status_reg', 'high', 'cmd 0x05'),
    0x40008820: ('i2c_bus_delay', 'medium', 'called with 8/5/0 around every I2C transaction'),
    0x40008B78: ('watchdog_kick', 'high', 'toggles GPIO 0x26 once per second'),
    0x40008BB4: ('cmd_reply_len_helper', 'proven', 'writes status byte and BE32 length into the reply'),
    0x40008D70: ('ep1_command_dispatcher', 'proven', '45 opcodes on BE16(hdr+2)/BE32(hdr+4)'),
    0x4000B22C: ('enter_firmware_update_mode', 'proven', '"Firmware size is %d, update starting..."'),
    0x4000B320: ('firmware_update_record', 'proven', 'erase/program/verify 4 KB, A/B app-flag flip'),
    0x4000BE60: ('ep2_command_dispatcher', 'proven', '3-entry filter table at 0x40030324 + update mode'),
    0x4000C290: ('get_fpga_load_addr', 'proven', '"GetFpgaLoadAddr: 0x%x"; 0x80000/0x200000'),
    0x4000D748: ('exposure_gain_helper', 'low', 'reached from opcodes 0x1026/0x1027'),
    0x400103D4: ('i2c_read', 'high', 'CyU3PI2cReceiveBytes shape (preamble, buf, count)'),
    0x400108A0: ('i2c_write', 'high', 'CyU3PI2cTransmitBytes shape (preamble, buf, count)'),
    0x40010E34: ('spi_receive', 'high', 'SPI controller at 0xE0000C00, RX path'),
    0x400114E4: ('spi_transmit', 'high', 'SPI controller at 0xE0000C00, TX path'),
    0x4001145C: ('spi_slave_select', 'high', 'chip-select assert/deassert for device 0/1'),
    0x40011E00: ('dma_channel_create', 'high', 'CyU3PDmaChannelCreate(channel,type,cfg)'),
    0x40012534: ('dma_discard_buffer', 'high', 'called right after GetBuffer in both thread loops'),
    0x40012884: ('dma_get_buffer', 'high', 'CyU3PDmaChannelGetBuffer(ch,&buf,wait)'),
    0x40012EC0: ('dma_send_buffer', 'high', 'CyU3PDmaChannelSetupSendBuffer(ch,buf,len)'),
    0x40013074: ('dma_set_xfer', 'high', 'CyU3PDmaChannelSetXfer(ch,0)'),
    0x40014018: ('debug_printf', 'high', 'every format string in the image is passed here'),
    0x40014D50: ('system_reboot', 'high', 'writes entry to 0x40000000 then resets'),
    0x40019130: ('usb_connect', 'high', 'CyU3PUsbConnect(0/1,0/1)'),
    0x40019524: ('usb_set_ep_config', 'high', 'CyU3PSetEpConfig(ep,&cfg)'),
    0x40019E18: ('usb_ack_setup', 'high', 'CyU3PUsbAckSetup()'),
    0x4001A084: ('usb_flush_ep', 'high', 'CyU3PUsbFlushEp(ep)'),
    0x4001B3A8: ('usb_set_desc', 'high', 'CyU3PUsbSetDesc(type,index,ptr)'),
    0x4001B570: ('usb_stall_ep', 'high', 'CyU3PUsbStall(ep,dir)'),
    0x4001BD10: ('usb_start', 'high', 'CyU3PUsbStart()'),
    0x4001ECF8: ('usb_register_event_cb', 'high', 'installs FUN_40003E88'),
    0x4001ED5C: ('usb_register_setup_cb', 'high', 'installs FUN_40004210'),
    0x400205E0: ('usb_ep_event_slots', 'medium', 'programs 0x40033420[] and calls FUN_40020148 per EP'),
    0x40021C04: ('thread_sleep', 'high', 'tx_thread_sleep(ticks)'),
    0x400223CC: ('tx_timer_initialize', 'high', 'creates "System Timer Thread", TX_TIMER_ID'),
    0x40023850: ('thread_create_wrapper', 'high', 'CyU3PThreadCreate: TCB 0xA8, ThreadX status codes'),
    0x40023BC8: ('thread_relinquish', 'high', 'tx_thread_relinquish()'),
    0x40023BFC: ('thread_resume_or_event', 'medium', 'used to start comThread/ControlThread'),
    0x40027A20: ('tx_thread_create', 'high', 'ThreadX native, 10 args, TX_THREAD_ID immediate'),
}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--md'); ap.add_argument('--csv'); ap.add_argument('--json')
    ap.add_argument('--top', type=int, default=0)
    args = ap.parse_args()

    rows = []
    for a in STARTS:
        f = FUNCS[a]
        strs = sorted({(w, t) for _, w, t in STRREFS.get(a, [])})
        rows.append(dict(addr=a, name=f['name'], size=f['size'], lo=f['lo'], hi=f['hi'],
                         strings=[{'addr': w, 'text': t} for w, t in strs],
                         calls=[c for c in f['calls'] if c in FUNCS and c != a],
                         dats=f['dats'], consts=constants(f['src'])))
    if args.top:
        rows = sorted(rows, key=lambda r: (-len(r['strings']), -r['size']))[:args.top]
    rows.sort(key=lambda r: r['addr'])

    def role_of(a):
        r = ROLES.get(a)
        return (r[0], r[1], r[2]) if r else ('', '', '')

    if args.json:
        nodes = []
        for r in rows:
            role, conf, ev = role_of(r['addr'])
            nodes.append({'addr': r['addr'], 'ghidra_name': r['name'], 'inferred_role': role,
                          'confidence': conf, 'evidence': ev, 'size': r['size'],
                          'strings': [x['text'] for x in r['strings']],
                          'callees': r['calls']})
        edges = [{'from': r['addr'], 'to': c} for r in rows for c in r['calls']]
        json.dump({'arch': 'ARM926EJ-S', 'entry': ENTRY,
                   'note': 'edges are direct calls recovered from the decompiled corpus; '
                           'self-recursion and unresolved indirect calls are excluded',
                   'nodes': nodes, 'edges': edges}, open(args.json, 'w'), indent=1)
        print('wrote %s (%d nodes, %d edges)' % (args.json, len(nodes), len(edges)), file=sys.stderr)

    if args.csv:
        import csv
        w = csv.writer(open(args.csv, 'w', newline=''))
        w.writerow(['address', 'ghidra_name', 'inferred_role', 'confidence', 'evidence'])
        for r in rows:
            role, conf, ev = role_of(r['addr'])
            w.writerow(['0x%08X' % r['addr'], r['name'], role, conf, ev])
        print('wrote %s' % args.csv, file=sys.stderr)

    out = ['| addr | size | inferred role | strings referenced (addr) | callees |',
           '|---|---|---|---|---|']
    for r in rows:
        role, conf, _ = role_of(r['addr'])
        s = '; '.join('%s@0x%x' % (x['text'][:36].replace('|', '/'), x['addr']) for x in r['strings'][:5])
        c = ', '.join('0x%x' % x for x in r['calls'][:8])
        out.append('| 0x%08x | %d | %s%s | %s | %s |' % (r['addr'], r['size'], role,
                                                          (' (%s)' % conf) if conf else '', s, c))
    text = '\n'.join(out)
    if args.md:
        open(args.md, 'w').write(text + '\n')
        print('wrote %s' % args.md, file=sys.stderr)
    else:
        print(text)

if __name__ == '__main__':
    main()
