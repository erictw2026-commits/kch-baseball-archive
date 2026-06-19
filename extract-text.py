#!/usr/bin/env python3
"""
SWF text + image extractor for index2-5.swf.

抽出：
- DefineFont3 的 codeTable (glyph index → Unicode)
- DefineText 的文字記錄 (用 codeTable 還原)
- PlaceObject / ShowFrame：判斷每個 frame 顯示哪張 JPEG + 哪些 text

輸出：
- frames.json: [{frame: N, image_id: M, texts: ["..."]}]
"""

import io
import json
import struct
import sys
import zlib
from pathlib import Path

SWF_PATH = Path("/Users/ericyu/claudecode/kch-baseball-archive/story/index2-5.swf")
OUT_JSON = Path("/Users/ericyu/claudecode/kch-baseball-archive/frames.json")


# ---------- Bit / Byte readers ----------

class ByteReader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def u8(self):
        v = self.data[self.pos]
        self.pos += 1
        return v

    def u16(self):
        v = struct.unpack_from("<H", self.data, self.pos)[0]
        self.pos += 2
        return v

    def s16(self):
        v = struct.unpack_from("<h", self.data, self.pos)[0]
        self.pos += 2
        return v

    def u32(self):
        v = struct.unpack_from("<I", self.data, self.pos)[0]
        self.pos += 4
        return v

    def s32(self):
        v = struct.unpack_from("<i", self.data, self.pos)[0]
        self.pos += 4
        return v

    def read(self, n):
        v = self.data[self.pos:self.pos + n]
        self.pos += n
        return v

    def skip(self, n):
        self.pos += n

    def remaining(self):
        return len(self.data) - self.pos


class BitReader:
    """Bit-aligned reader for SWF (MSB first)."""
    def __init__(self, data: bytes, start_byte: int = 0):
        self.data = data
        self.byte_pos = start_byte
        self.bit_pos = 0  # 0..7, 0 = MSB

    def read_ub(self, n: int) -> int:
        """Unsigned bits."""
        v = 0
        while n > 0:
            if self.byte_pos >= len(self.data):
                # past end => zeros
                return v << n
            byte = self.data[self.byte_pos]
            bits_left = 8 - self.bit_pos
            take = min(bits_left, n)
            shift = bits_left - take
            chunk = (byte >> shift) & ((1 << take) - 1)
            v = (v << take) | chunk
            self.bit_pos += take
            if self.bit_pos == 8:
                self.bit_pos = 0
                self.byte_pos += 1
            n -= take
        return v

    def read_sb(self, n: int) -> int:
        """Signed bits (two's complement)."""
        if n == 0:
            return 0
        v = self.read_ub(n)
        if v & (1 << (n - 1)):
            v -= 1 << n
        return v

    def align_byte(self):
        if self.bit_pos:
            self.bit_pos = 0
            self.byte_pos += 1

    def tell_byte(self):
        return self.byte_pos


def read_rect(br: BitReader):
    nbits = br.read_ub(5)
    xmin = br.read_sb(nbits)
    xmax = br.read_sb(nbits)
    ymin = br.read_sb(nbits)
    ymax = br.read_sb(nbits)
    br.align_byte()
    return (xmin, xmax, ymin, ymax)


# ---------- SWF top-level parse ----------

def load_swf(path):
    raw = path.read_bytes()
    sig = raw[:3]
    version = raw[3]
    file_len = struct.unpack_from("<I", raw, 4)[0]
    body = raw[8:]
    if sig == b"CWS":
        body = zlib.decompress(body)
    elif sig == b"FWS":
        pass
    elif sig == b"ZWS":
        raise RuntimeError("LZMA not supported")
    else:
        raise RuntimeError(f"Unknown sig: {sig!r}")
    # body now is the uncompressed data starting at SWF header rect
    return sig, version, file_len, body


def parse_header(body):
    br = BitReader(body, 0)
    rect = read_rect(br)
    pos = br.tell_byte()
    # next: framerate UI16 (LE, but it's actually fixed-point 8.8 little endian)
    framerate = struct.unpack_from("<H", body, pos)[0]
    framecount = struct.unpack_from("<H", body, pos + 2)[0]
    return rect, framerate, framecount, pos + 4


# ---------- Tag iteration ----------

def iter_tags(body, start):
    rdr = ByteReader(body)
    rdr.pos = start
    while rdr.remaining() > 0:
        if rdr.remaining() < 2:
            break
        tag_code_and_length = rdr.u16()
        tag_code = tag_code_and_length >> 6
        tag_length = tag_code_and_length & 0x3F
        if tag_length == 0x3F:
            if rdr.remaining() < 4:
                break
            tag_length = rdr.u32()
        if rdr.remaining() < tag_length:
            break
        tag_data = rdr.read(tag_length)
        yield tag_code, tag_data
        if tag_code == 0:  # End
            break


# ---------- DefineFont3 ----------

def parse_define_font3(data):
    """
    DefineFont3 (Tag 75):
      FontID: UI16
      FontFlagsHasLayout: UB[1]
      FontFlagsShiftJIS: UB[1]
      FontFlagsSmallText: UB[1]
      FontFlagsANSI: UB[1]
      FontFlagsWideOffsets: UB[1]  (DefineFont3 always wide offsets actually, but flag exists)
      FontFlagsWideCodes: UB[1]    (always 1 in v3)
      FontFlagsItalic: UB[1]
      FontFlagsBold: UB[1]
      LanguageCode: UI8
      FontNameLen: UI8
      FontName: UI8 * len
      NumGlyphs: UI16
      OffsetTable: NumGlyphs * UI32  (DefineFont3 uses UI32 if WideOffsets=1; SPEC says DefineFont3 always uses 32-bit; sample reveals: in practice it's the FontFlagsWideOffsets bit that decides, default is wide for v3)
      CodeTableOffset: UI32
      [GlyphShapeTable]
      CodeTable: NumGlyphs * UI16
    """
    rdr = ByteReader(data)
    font_id = rdr.u16()
    flags = rdr.u8()
    has_layout = (flags >> 7) & 1
    shift_jis = (flags >> 6) & 1
    small = (flags >> 5) & 1
    ansi = (flags >> 4) & 1
    wide_offsets = (flags >> 3) & 1
    wide_codes = (flags >> 2) & 1
    italic = (flags >> 1) & 1
    bold = flags & 1
    language = rdr.u8()
    name_len = rdr.u8()
    name = rdr.read(name_len).decode("utf-8", errors="replace")
    num_glyphs = rdr.u16()
    if num_glyphs == 0:
        return font_id, name, {}

    # Save offset table start
    offset_table_start = rdr.pos

    # Read offsets to know where each glyph shape begins (relative to offset_table_start)
    if wide_offsets:
        offsets = [rdr.u32() for _ in range(num_glyphs)]
        code_table_offset = rdr.u32()
    else:
        offsets = [rdr.u16() for _ in range(num_glyphs)]
        code_table_offset = rdr.u16()

    # Seek to code table: it is at offset_table_start + code_table_offset
    code_table_pos = offset_table_start + code_table_offset
    rdr.pos = code_table_pos

    code_table = []
    for _ in range(num_glyphs):
        if wide_codes:
            code_table.append(rdr.u16())
        else:
            code_table.append(rdr.u8())

    # Map glyph index -> unicode char
    glyph_map = {}
    for idx, cp in enumerate(code_table):
        try:
            glyph_map[idx] = chr(cp)
        except ValueError:
            glyph_map[idx] = "?"

    return font_id, name, glyph_map


# ---------- DefineFont2 (Tag 48) ----------

def parse_define_font2(data):
    rdr = ByteReader(data)
    font_id = rdr.u16()
    flags = rdr.u8()
    has_layout = (flags >> 7) & 1
    shift_jis = (flags >> 6) & 1
    small = (flags >> 5) & 1
    ansi = (flags >> 4) & 1
    wide_offsets = (flags >> 3) & 1
    wide_codes = (flags >> 2) & 1
    italic = (flags >> 1) & 1
    bold = flags & 1
    language = rdr.u8()
    name_len = rdr.u8()
    name = rdr.read(name_len).decode("utf-8", errors="replace")
    num_glyphs = rdr.u16()
    if num_glyphs == 0:
        return font_id, name, {}
    offset_table_start = rdr.pos
    if wide_offsets:
        offsets = [rdr.u32() for _ in range(num_glyphs)]
        code_table_offset = rdr.u32()
    else:
        offsets = [rdr.u16() for _ in range(num_glyphs)]
        code_table_offset = rdr.u16()
    code_table_pos = offset_table_start + code_table_offset
    rdr.pos = code_table_pos
    code_table = []
    for _ in range(num_glyphs):
        if wide_codes:
            code_table.append(rdr.u16())
        else:
            code_table.append(rdr.u8())
    glyph_map = {}
    for idx, cp in enumerate(code_table):
        try:
            glyph_map[idx] = chr(cp)
        except ValueError:
            glyph_map[idx] = "?"
    return font_id, name, glyph_map


# ---------- DefineText (Tag 11) / DefineText2 (Tag 33) ----------

def parse_define_text(data, fonts):
    """
    DefineText:
      CharacterID: UI16
      TextBounds: RECT
      TextMatrix: MATRIX
      GlyphBits: UI8
      AdvanceBits: UI8
      TextRecords...
      End: UI8 = 0
    """
    rdr = ByteReader(data)
    char_id = rdr.u16()
    # Read RECT (bit-aligned)
    br = BitReader(data, rdr.pos)
    read_rect(br)
    # Read MATRIX
    read_matrix(br)
    rdr.pos = br.tell_byte()
    glyph_bits = rdr.u8()
    advance_bits = rdr.u8()

    pieces = []
    current_font_id = None
    current_glyph_map = None

    while True:
        if rdr.remaining() < 1:
            break
        flags = rdr.u8()
        if flags == 0:
            break
        # Type 1: text record
        # bit 8 (0x80) is always 1 for type1, then 4 zeros, then has_font/color/y/x bits
        type_flag = (flags >> 7) & 1
        has_font = (flags >> 3) & 1
        has_color = (flags >> 2) & 1
        has_y = (flags >> 1) & 1
        has_x = flags & 1

        if has_font:
            font_id = rdr.u16()
            current_font_id = font_id
            current_glyph_map = fonts.get(font_id)
        if has_color:
            # DefineText: RGB (3 bytes); DefineText2: RGBA (4). We assume DefineText (tag 11).
            rdr.skip(3)
        if has_x:
            rdr.s16()
        if has_y:
            rdr.s16()
        if has_font:
            rdr.u16()  # text height

        glyph_count = rdr.u8()
        # Now bit-aligned glyph records
        br = BitReader(data, rdr.pos)
        text_chunk = []
        for _ in range(glyph_count):
            gi = br.read_ub(glyph_bits)
            ga = br.read_sb(advance_bits)
            if current_glyph_map is not None and gi in current_glyph_map:
                text_chunk.append(current_glyph_map[gi])
            else:
                text_chunk.append("?")
        br.align_byte()
        rdr.pos = br.tell_byte()
        pieces.append("".join(text_chunk))

    return char_id, pieces


def parse_define_text2(data, fonts):
    """Same as DefineText but RGBA for colors."""
    rdr = ByteReader(data)
    char_id = rdr.u16()
    br = BitReader(data, rdr.pos)
    read_rect(br)
    read_matrix(br)
    rdr.pos = br.tell_byte()
    glyph_bits = rdr.u8()
    advance_bits = rdr.u8()

    pieces = []
    current_glyph_map = None

    while True:
        if rdr.remaining() < 1:
            break
        flags = rdr.u8()
        if flags == 0:
            break
        has_font = (flags >> 3) & 1
        has_color = (flags >> 2) & 1
        has_y = (flags >> 1) & 1
        has_x = flags & 1

        if has_font:
            font_id = rdr.u16()
            current_glyph_map = fonts.get(font_id)
        if has_color:
            rdr.skip(4)  # RGBA
        if has_x:
            rdr.s16()
        if has_y:
            rdr.s16()
        if has_font:
            rdr.u16()

        glyph_count = rdr.u8()
        br = BitReader(data, rdr.pos)
        text_chunk = []
        for _ in range(glyph_count):
            gi = br.read_ub(glyph_bits)
            ga = br.read_sb(advance_bits)
            if current_glyph_map is not None and gi in current_glyph_map:
                text_chunk.append(current_glyph_map[gi])
            else:
                text_chunk.append("?")
        br.align_byte()
        rdr.pos = br.tell_byte()
        pieces.append("".join(text_chunk))

    return char_id, pieces


def read_matrix(br: BitReader):
    has_scale = br.read_ub(1)
    if has_scale:
        nbits = br.read_ub(5)
        br.read_sb(nbits)  # scaleX
        br.read_sb(nbits)  # scaleY
    has_rotate = br.read_ub(1)
    if has_rotate:
        nbits = br.read_ub(5)
        br.read_sb(nbits)  # rotateSkew0
        br.read_sb(nbits)  # rotateSkew1
    nbits = br.read_ub(5)
    br.read_sb(nbits)  # translateX
    br.read_sb(nbits)  # translateY
    br.align_byte()


# ---------- PlaceObject2 (Tag 26) ----------

def parse_place_object2(data):
    rdr = ByteReader(data)
    flags = rdr.u8()
    depth = rdr.u16()
    has_clip = (flags >> 7) & 1
    has_name = (flags >> 6) & 1
    has_ratio = (flags >> 5) & 1
    has_color = (flags >> 4) & 1
    has_matrix = (flags >> 3) & 1
    has_char = (flags >> 2) & 1
    is_move = (flags >> 1) & 1
    char_id = None
    if has_char:
        char_id = rdr.u16()
    return depth, char_id, is_move


# ---------- DefineSprite (Tag 39) ----------
# A sprite is a mini-SWF: it has its own frame timeline (PlaceObject + ShowFrame)
# Format: SpriteID UI16, FrameCount UI16, then a sequence of control tags ending with End(0).

def parse_define_sprite(data, fonts, define_texts_by_id):
    """Return list of frames inside sprite: [{frame, image_id, text_ids}]."""
    rdr = ByteReader(data)
    sprite_id = rdr.u16()
    frame_count = rdr.u16()

    rest = data[rdr.pos:]
    sub_frames = walk_frames(rest, fonts, define_texts_by_id, parent_label=f"sprite{sprite_id}")
    return sprite_id, frame_count, sub_frames


# ---------- Frame walker ----------

def walk_frames(body_segment, fonts, define_texts_by_id, parent_label=""):
    """Walk a tag stream, tracking PlaceObject and ShowFrame. Return list of frames per ShowFrame."""
    frame_idx = 0
    # depth -> char_id (current displayed)
    display_list = {}
    frames_out = []

    rdr = ByteReader(body_segment)
    while rdr.remaining() > 0:
        if rdr.remaining() < 2:
            break
        tcl = rdr.u16()
        code = tcl >> 6
        length = tcl & 0x3F
        if length == 0x3F:
            if rdr.remaining() < 4:
                break
            length = rdr.u32()
        if rdr.remaining() < length:
            break
        tdata = rdr.read(length)
        if code == 0:
            break
        elif code == 4:
            # PlaceObject (v1): UI16 char_id, UI16 depth, MATRIX, [CXFORM]
            depth = struct.unpack_from("<H", tdata, 2)[0]
            char_id = struct.unpack_from("<H", tdata, 0)[0]
            display_list[depth] = char_id
        elif code == 26:
            depth, char_id, is_move = parse_place_object2(tdata)
            if char_id is not None:
                display_list[depth] = char_id
        elif code == 70:
            # PlaceObject3 - similar header
            # flags2 UI8 + flags UI8 + depth UI16 ... we mainly need depth + char_id
            try:
                rdr2 = ByteReader(tdata)
                flags = rdr2.u8()
                flags2 = rdr2.u8()
                depth = rdr2.u16()
                has_char = (flags >> 2) & 1
                if (flags2 >> 4) & 1:
                    name_len = 0
                    # ClassName UTF8 zero-terminated
                    while True:
                        b = rdr2.u8()
                        if b == 0:
                            break
                if has_char:
                    char_id = rdr2.u16()
                    display_list[depth] = char_id
            except Exception:
                pass
        elif code == 5:
            # RemoveObject: char_id + depth
            depth = struct.unpack_from("<H", tdata, 2)[0]
            display_list.pop(depth, None)
        elif code == 28:
            # RemoveObject2: depth UI16
            depth = struct.unpack_from("<H", tdata, 0)[0]
            display_list.pop(depth, None)
        elif code == 1:
            # ShowFrame
            # Snapshot display list -> identify text + bitmap chars
            text_ids = [cid for cid in display_list.values() if cid in define_texts_by_id]
            # bitmaps: any char id we've seen as DefineBitsJPEG (tracked outside)
            frames_out.append({
                "frame": frame_idx,
                "display_list": dict(display_list),
                "text_ids": text_ids,
            })
            frame_idx += 1
    return frames_out


# ---------- Main ----------

def main():
    sig, ver, file_len, body = load_swf(SWF_PATH)
    rect, framerate, framecount, after_header = parse_header(body)
    print(f"SWF v{ver}, frames={framecount}, framerate={framerate / 256:.1f}, rect={rect}")

    fonts = {}                  # font_id -> glyph_map
    font_names = {}             # font_id -> name
    define_texts_by_id = {}     # char_id -> [strings]
    bitmap_ids = set()          # JPEG character ids
    define_sprites = {}         # sprite_id -> (frame_count, sub_frames)

    body_after_header = body[after_header:]

    # First pass: collect all DefineFont3 / DefineFont2 / DefineText / DefineBits / DefineSprite
    for code, tdata in iter_tags(body, after_header):
        if code == 75:
            try:
                fid, name, gmap = parse_define_font3(tdata)
                fonts[fid] = gmap
                font_names[fid] = name
            except Exception as e:
                print(f"  ! DefineFont3 fail: {e}")
        elif code == 48:
            try:
                fid, name, gmap = parse_define_font2(tdata)
                fonts[fid] = gmap
                font_names[fid] = name
            except Exception as e:
                print(f"  ! DefineFont2 fail: {e}")
        elif code in (6, 21, 35, 90):  # DefineBitsJPEG, JPEG2, JPEG3, JPEG4
            cid = struct.unpack_from("<H", tdata, 0)[0]
            bitmap_ids.add(cid)
        elif code == 39:  # DefineSprite
            try:
                sid, fc, sub_frames = parse_define_sprite(tdata, fonts, {})  # texts not yet known
                define_sprites[sid] = (fc, sub_frames, tdata)
            except Exception as e:
                print(f"  ! Sprite parse fail: {e}")

    print(f"Fonts: {list(fonts.keys())}, names={font_names}")
    print(f"Font glyph counts: { {fid: len(m) for fid, m in fonts.items()} }")
    print(f"Bitmaps: {len(bitmap_ids)}")
    print(f"Sprites: {len(define_sprites)}")

    # Second pass: parse texts now that fonts are loaded
    for code, tdata in iter_tags(body, after_header):
        if code == 11:
            try:
                cid, pieces = parse_define_text(tdata, fonts)
                define_texts_by_id[cid] = pieces
            except Exception as e:
                print(f"  ! DefineText fail: {e}")
        elif code == 33:
            try:
                cid, pieces = parse_define_text2(tdata, fonts)
                define_texts_by_id[cid] = pieces
            except Exception as e:
                print(f"  ! DefineText2 fail: {e}")

    print(f"DefineTexts: {len(define_texts_by_id)}")
    # Print first few extracted texts
    sample_texts = list(define_texts_by_id.items())[:8]
    for cid, pieces in sample_texts:
        joined = " | ".join(pieces)
        print(f"  text id={cid}: {joined!r}")

    # Walk frames again with text map populated
    main_frames = walk_frames(body_after_header, fonts, define_texts_by_id)
    print(f"Main timeline frames: {len(main_frames)}")

    # Parse sprites with texts now known
    sprite_frames_map = {}
    for sid, (fc, _stale, tdata) in define_sprites.items():
        rdr = ByteReader(tdata)
        _sid = rdr.u16()
        _fc = rdr.u16()
        rest = tdata[rdr.pos:]
        sf = walk_frames(rest, fonts, define_texts_by_id)
        sprite_frames_map[sid] = sf

    # Build per-frame report:
    # In each ShowFrame on main timeline, snapshot displayed bitmaps and texts.
    # Some texts may be inside sprites placed on main timeline; we expand sprite frames too.
    results = []
    for f in main_frames:
        bmps = [cid for cid in f["display_list"].values() if cid in bitmap_ids]
        texts = []
        for cid in f["display_list"].values():
            if cid in define_texts_by_id:
                texts.extend(define_texts_by_id[cid])
            elif cid in sprite_frames_map:
                # Aggregate every text id used in any sub-frame
                for sub in sprite_frames_map[cid]:
                    for sub_cid in sub["display_list"].values():
                        if sub_cid in define_texts_by_id:
                            texts.extend(define_texts_by_id[sub_cid])
        results.append({
            "frame": f["frame"] + 1,
            "bitmaps": bmps,
            "texts": texts,
        })

    # Print and save
    print("=" * 60)
    print("Per-frame extraction:")
    for r in results:
        print(f"Frame {r['frame']}: bmps={r['bitmaps']} texts(count={len(r['texts'])}):")
        for t in r["texts"]:
            print(f"    {t!r}")

    # Save sample of all texts for review
    out = {
        "summary": {
            "version": ver,
            "framecount_header": framecount,
            "main_frames": len(main_frames),
            "fonts": {fid: {"name": font_names.get(fid, ""), "glyphs": len(m)} for fid, m in fonts.items()},
            "bitmap_count": len(bitmap_ids),
            "text_count": len(define_texts_by_id),
        },
        "all_texts": {str(cid): pieces for cid, pieces in define_texts_by_id.items()},
        "frames": results,
    }
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\nWrote {OUT_JSON}")


if __name__ == "__main__":
    main()
