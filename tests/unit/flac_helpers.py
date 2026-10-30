"""Builders shared by the FLAC test modules."""


class BytesBackend:
    """In-memory backend for driving flac.py without a filesystem."""

    def __init__(self, data: bytes):
        self.data = data

    def read(self, uri):
        return self.data

    def read_range(self, uri, start_byte, end_byte):
        return self.data[start_byte:] if end_byte is None else self.data[start_byte:end_byte + 1]

    def size(self, uri):
        return len(self.data)


def build_streaminfo(
    *,
    sample_rate,
    channels,
    bits_per_sample,
    total_samples,
    md5=b"\x00" * 16,
    min_block=4096,
    max_block=4096,
):
    block = bytearray(34)
    block[0:2] = min_block.to_bytes(2, "big")
    block[2:4] = max_block.to_bytes(2, "big")
    packed = (
        (sample_rate << 44)
        | ((channels - 1) << 41)
        | ((bits_per_sample - 1) << 36)
        | total_samples
    )
    block[10:18] = packed.to_bytes(8, "big")
    block[18:34] = md5
    return bytes(block)


def build_metadata_region(streaminfo, extra_blocks=()):
    """fLaC marker + STREAMINFO + extra (type, body) blocks, flags per spec."""
    blocks = [(0, streaminfo), *extra_blocks]
    out = b"fLaC"
    for i, (block_type, body) in enumerate(blocks):
        last_flag = 0x80 if i == len(blocks) - 1 else 0
        out += bytes([last_flag | block_type]) + len(body).to_bytes(3, "big") + body
    return out
