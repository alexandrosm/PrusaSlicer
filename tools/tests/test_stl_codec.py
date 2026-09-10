import hashlib
from pathlib import Path
import random
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stl_codec as codec


def fixture(count=20, seed=7):
    rng = random.Random(seed)
    # Deliberately include signed zero, NaN payloads, infinities, and nonzero
    # attributes. Treating these values as floats would lose important bits.
    vertices = [struct.pack("<III", *bits) for bits in
                ((0, 0x80000000, 0x7fc00123), (0x7f800000, 1, 2), (3, 4, 5))]
    header = bytes(range(80)) + struct.pack("<I", count)
    return header + b"".join(rng.choice(vertices) + b"".join(rng.choice(vertices) for _ in range(3)) +
                             struct.pack("<H", rng.randrange(65536)) for _ in range(count))


class CodecTests(unittest.TestCase):
    def test_all_modes_exact_round_trip(self):
        for count in (0, 1, 2, 37, 500):
            original = fixture(count)
            for mode in codec.MODES:
                with self.subTest(count=count, mode=mode):
                    self.assertEqual(codec.decode(codec.encode(original, mode)), original)

    def test_random_bits_round_trip(self):
        rng = random.Random(42)
        for count in range(20):
            original = rng.randbytes(80) + struct.pack("<I", count) + rng.randbytes(50 * count)
            for mode in codec.MODES:
                self.assertEqual(codec.decode(codec.encode(original, mode)), original)

    def test_reject_ascii_and_trailing_bytes(self):
        for original in (b"solid fixture\nendsolid\n", fixture(1) + b"trailing", b""):
            with self.assertRaises(ValueError):
                codec.encode(original)

    def test_malformed_headers(self):
        encoded = codec.encode(fixture())
        variants = [encoded[:10], b"UNKNOWN!" + encoded[8:], encoded + b"x",
                    encoded[:9] + b"\x01" + encoded[10:], encoded[:8] + b"\xff" + encoded[9:],
                    encoded[:12] + struct.pack("<Q", 2**63) + encoded[20:]]
        for value in variants:
            with self.subTest(length=len(value)):
                with self.assertRaises(ValueError):
                    codec.decode(value)

    def test_corruption_detected(self):
        for mode in codec.MODES:
            encoded = bytearray(codec.encode(fixture(), mode))
            encoded[-1] ^= 0x80
            with self.assertRaises(ValueError):
                codec.decode(encoded)

    def test_out_of_range_indices(self):
        encoded = bytearray(codec.encode(fixture(1), "indexed"))
        start = codec.HEADER.size + 84
        vertex_count, normal_count = struct.unpack_from("<II", encoded, start)
        index_start = start + 8 + vertex_count * 12 + normal_count * 12
        encoded[index_start:index_start + 12] = b"\xff" * 12
        with self.assertRaisesRegex(ValueError, "index out of bounds"):
            codec.decode(encoded)

    def test_huge_dictionary_rejected_without_allocation(self):
        encoded = bytearray(codec.encode(fixture(1)))
        struct.pack_into("<I", encoded, codec.HEADER.size + 84, 0xffffffff)
        with self.assertRaisesRegex(ValueError, "Oversized"):
            codec.decode(encoded)

    def test_shuffle_is_inverse(self):
        for width in (2, 4, 12, 50):
            source = bytes(range(width)) * 8
            self.assertEqual(codec.unshuffle(codec.shuffle(source, width), width), source)
        with self.assertRaises(ValueError):
            codec.shuffle(b"x", 4)


if __name__ == "__main__":
    unittest.main()
