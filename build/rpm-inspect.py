#!/usr/bin/env python3
"""Minimal RPM reader — enough of `rpm -qlp` / `rpm -qp --scripts` to VERIFY a
package we just built, on a box that has no rpm and no bsdtar (this fleet's
build hosts are Ubuntu; the rpm target is produced by electron-builder's
bundled fpm, so `rpm` itself is never installed).

Everything comes from the RPM *header*, which is a plain tag/value blob — no
payload decompression needed for the file list. `--payload` streams the
decompressed cpio for the one case that does need it (pulling app.asar back out
to check its version).

    rpm-inspect.py --files    pkg.rpm   -> one absolute path per line
    rpm-inspect.py --meta     pkg.rpm   -> name/version/release/arch/requires
    rpm-inspect.py --scripts  pkg.rpm   -> POSTIN/POSTUN bodies (presence check)
    rpm-inspect.py --payload  pkg.rpm   -> decompressed cpio archive on stdout

Format reference: lead (96 bytes) | signature header | header | payload.
Each header: 8-byte magic+reserved, nindex(u32), hsize(u32), nindex*16 bytes of
index entries, hsize bytes of data. The signature header is padded to an
8-byte boundary; the main header is not.
"""
import struct
import subprocess
import sys

HEADER_MAGIC = b"\x8e\xad\xe8\x01"
LEAD_MAGIC = b"\xed\xab\xee\xdb"

# tag -> name (only the ones we use). Numbers from rpm's rpmtag.h — note that
# the scriptlet block is PREIN 1023 / POSTIN 1024 / PREUN 1025 / POSTUN 1026,
# which is one off from the obvious guess.
TAGS = {
    1000: "NAME", 1001: "VERSION", 1002: "RELEASE", 1022: "ARCH",
    1023: "PREIN", 1024: "POSTIN", 1025: "PREUN", 1026: "POSTUN",
    1047: "PROVIDENAME", 1049: "REQUIRENAME", 1054: "CONFLICTNAME",
    1090: "OBSOLETENAME",
    1116: "DIRINDEXES", 1117: "BASENAMES", 1118: "DIRNAMES", 1030: "FILEMODES",
}

TYPE_NULL, TYPE_CHAR, TYPE_INT8, TYPE_INT16, TYPE_INT32, TYPE_INT64 = range(6)
TYPE_STRING, TYPE_BIN, TYPE_STRING_ARRAY, TYPE_I18NSTRING = 6, 7, 8, 9


def _read_header(buf, off):
    """Parse one header at `off`; return (tags dict, offset just past it)."""
    if buf[off:off + 4] != HEADER_MAGIC:
        raise ValueError(f"no rpm header magic at offset {off}")
    nindex, hsize = struct.unpack(">II", buf[off + 8:off + 16])
    index_off = off + 16
    data_off = index_off + nindex * 16
    tags = {}
    for i in range(nindex):
        tag, typ, doff, count = struct.unpack(
            ">IIiI", buf[index_off + i * 16:index_off + (i + 1) * 16])
        tags[tag] = _read_value(buf, data_off + doff, typ, count)
    return tags, data_off + hsize


def _read_value(buf, off, typ, count):
    if typ in (TYPE_STRING, TYPE_I18NSTRING, TYPE_STRING_ARRAY):
        out = []
        for _ in range(count):
            end = buf.index(b"\0", off)
            out.append(buf[off:end].decode("utf-8", "replace"))
            off = end + 1
        return out[0] if typ == TYPE_STRING else out
    if typ == TYPE_INT32:
        return list(struct.unpack(f">{count}i", buf[off:off + 4 * count]))
    if typ == TYPE_INT16:
        return list(struct.unpack(f">{count}h", buf[off:off + 2 * count]))
    if typ == TYPE_INT8 or typ == TYPE_CHAR:
        return list(buf[off:off + count])
    if typ == TYPE_INT64:
        return list(struct.unpack(f">{count}q", buf[off:off + 8 * count]))
    if typ == TYPE_BIN:
        return buf[off:off + count]
    return None


def parse(path):
    with open(path, "rb") as fh:
        buf = fh.read()
    if buf[:4] != LEAD_MAGIC:
        raise ValueError(f"{path}: not an RPM (bad lead magic)")
    _sig, off = _read_header(buf, 96)
    off = (off + 7) & ~7                      # signature header is 8-aligned
    hdr, payload_off = _read_header(buf, off)
    return {TAGS.get(t, t): v for t, v in hdr.items()}, buf[payload_off:]


def file_list(hdr):
    basenames = hdr.get("BASENAMES") or []
    dirnames = hdr.get("DIRNAMES") or []
    dirindexes = hdr.get("DIRINDEXES") or []
    return [dirnames[dirindexes[i]] + basenames[i] for i in range(len(basenames))]


def decompress(payload):
    """RPM payloads are cpio under gzip/xz/zstd/bzip2 — sniff and shell out."""
    if payload[:2] == b"\x1f\x8b":
        import gzip
        return gzip.decompress(payload)
    if payload[:6] == b"\xfd7zXZ\x00":
        import lzma
        return lzma.decompress(payload)
    if payload[:3] == b"BZh":
        import bz2
        return bz2.decompress(payload)
    if payload[:4] == b"\x28\xb5\x2f\xfd":
        # zstd landed in the stdlib only in 3.14; the CLI is everywhere.
        try:
            import compression.zstd as _z          # type: ignore[import]
            return _z.decompress(payload)
        except Exception:
            return subprocess.run(["zstd", "-dc"], input=payload,
                                  stdout=subprocess.PIPE, check=True).stdout
    raise ValueError(f"unknown payload compression: {payload[:8]!r}")


def main(argv):
    if len(argv) != 3:
        sys.exit(__doc__)
    mode, path = argv[1], argv[2]
    hdr, payload = parse(path)
    if mode == "--files":
        print("\n".join(file_list(hdr)))
    elif mode == "--meta":
        for key in ("NAME", "VERSION", "RELEASE", "ARCH"):
            print(f"{key}: {hdr.get(key)}")
        for key in ("REQUIRENAME", "PROVIDENAME", "CONFLICTNAME", "OBSOLETENAME"):
            print(f"{key}: {' '.join(hdr.get(key) or [])}")
    elif mode == "--scripts":
        for key in ("POSTIN", "POSTUN"):
            body = hdr.get(key)
            print(f"===== {key} ({0 if not body else len(body)} bytes) =====")
            if body:
                print(body)
    elif mode == "--payload":
        sys.stdout.buffer.write(decompress(payload))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
