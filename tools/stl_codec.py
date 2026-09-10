#!/usr/bin/env python3
"""Experimental byte-reversible binary-STL preconditioner (standard library only).

This is not a geometry codec: all float bits, facet order, normals, attributes,
and the 80-byte STL header survive exactly. ASCII/malformed STLs are not changed.
Compression is deliberately external, allowing a whole-package LZMA2 comparison.
The small Windows decoder in stl_restore.cpp implements the same versioned format.
"""

import array
import hashlib
import struct
import sys


MAGIC = b"PSSTL1\0\0"
HEADER = struct.Struct("<8sB3xQ32s")
MAX_SOURCE_BYTES = 256 * 1024 * 1024
MODES = {"planes": 1, "indexed": 2, "indexed-xor": 3, "indexed-delta": 4}


def binary_count(data):
    if len(data) < 84 or len(data) > MAX_SOURCE_BYTES:
        raise ValueError("Unsupported STL length")
    count = struct.unpack_from("<I", data, 80)[0]
    if 84 + 50 * count != len(data):
        raise ValueError("Not an exact-length binary STL (ASCII/trailing bytes retained unchanged)")
    return count


def shuffle(data, width):
    if len(data) % width:
        raise ValueError("Incomplete record")
    return b"".join(data[i::width] for i in range(width))


def unshuffle(data, width):
    if len(data) % width:
        raise ValueError("Incomplete shuffled record")
    count = len(data) // width
    output = bytearray(len(data))
    for i in range(width):
        output[i::width] = data[i * count:(i + 1) * count]
    return bytes(output)


def uint_bytes(values):
    words = array.array("I", values)
    if words.itemsize != 4:
        raise RuntimeError("Codec needs 32-bit unsigned int")
    if sys.byteorder != "little":
        words.byteswap()
    return words.tobytes()


def uints(data):
    words = array.array("I")
    words.frombytes(data)
    if words.itemsize != 4:
        raise RuntimeError("Codec needs 32-bit unsigned int")
    if sys.byteorder != "little":
        words.byteswap()
    return words


def xor_words(data, stride=3, inverse=False):
    words = uints(data)
    previous = [0] * stride
    for i, word in enumerate(words):
        slot = i % stride
        decoded = word ^ previous[slot]
        previous[slot] = decoded if inverse else word
        words[i] = decoded
    if sys.byteorder != "little":
        words.byteswap()
    return words.tobytes()


def delta_indices(data, inverse=False):
    words = uints(data)
    previous = 0
    for i, word in enumerate(words):
        if inverse:
            decoded = previous + ((word >> 1) ^ -(word & 1))
            if not 0 <= decoded <= 0xffffffff:
                raise ValueError("Invalid delta index")
            previous = decoded
            words[i] = decoded
        else:
            delta = word - previous
            previous = word
            if not -0x80000000 <= delta < 0x80000000:
                raise ValueError("Index delta exceeds supported bound")
            words[i] = (delta << 1) ^ (delta >> 31)
    if sys.byteorder != "little":
        words.byteswap()
    return words.tobytes()


def encode(data, mode="indexed-xor"):
    count = binary_count(data)
    if mode not in MODES:
        raise ValueError("Unknown STL transform")
    prefix = HEADER.pack(MAGIC, MODES[mode], len(data), hashlib.sha256(data).digest())
    if mode == "planes":
        return prefix + data[:84] + shuffle(data[84:], 50)
    vertices, normals = {}, {}
    vertex_refs, normal_refs = array.array("I"), array.array("I")
    attributes = bytearray()
    for offset in range(84, len(data), 50):
        normal = data[offset:offset + 12]
        normal_refs.append(normals.setdefault(normal, len(normals)))
        for start in (offset + 12, offset + 24, offset + 36):
            vertex = data[start:start + 12]
            vertex_refs.append(vertices.setdefault(vertex, len(vertices)))
        attributes.extend(data[offset + 48:offset + 50])
    blocks = [b"".join(vertices), b"".join(normals),
              uint_bytes(vertex_refs), uint_bytes(normal_refs), bytes(attributes)]
    if mode == "indexed-xor":
        blocks = [xor_words(blocks[0]), xor_words(blocks[1]),
                  xor_words(blocks[2], 1), xor_words(blocks[3], 1), blocks[4]]
    elif mode == "indexed-delta":
        blocks = [xor_words(blocks[0]), xor_words(blocks[1]),
                  delta_indices(blocks[2]), delta_indices(blocks[3]), blocks[4]]
    widths = (12, 12, 4, 4, 2)
    return (prefix + data[:84] + struct.pack("<II", len(vertices), len(normals)) +
            b"".join(shuffle(block, width) for block, width in zip(blocks, widths)))


def decode(encoded):
    if len(encoded) < HEADER.size + 84:
        raise ValueError("Truncated codec header")
    magic, mode, size, digest = HEADER.unpack_from(encoded)
    if magic != MAGIC or mode not in MODES.values():
        raise ValueError("Unknown codec magic/version/mode")
    if encoded[9:12] != b"\0\0\0":
        raise ValueError("Nonzero reserved header bytes")
    if not 84 <= size <= MAX_SOURCE_BYTES or (size - 84) % 50:
        raise ValueError("Invalid reconstructed length")
    offset = HEADER.size
    stl_header = encoded[offset:offset + 84]
    offset += 84
    count = struct.unpack_from("<I", stl_header, 80)[0]
    if 84 + count * 50 != size:
        raise ValueError("Contradictory triangle count")
    if mode == 1:
        if len(encoded) != HEADER.size + size:
            raise ValueError("Truncated or trailing plane data")
        result = stl_header + unshuffle(encoded[offset:], 50)
    else:
        if len(encoded) < offset + 8:
            raise ValueError("Truncated dictionary sizes")
        vertex_count, normal_count = struct.unpack_from("<II", encoded, offset)
        offset += 8
        if vertex_count > count * 3 or normal_count > count:
            raise ValueError("Oversized dictionary")
        sizes = (vertex_count * 12, normal_count * 12, count * 12, count * 4, count * 2)
        if len(encoded) != offset + sum(sizes):
            raise ValueError("Truncated or trailing dictionary data")
        blocks = []
        for length, width in zip(sizes, (12, 12, 4, 4, 2)):
            blocks.append(unshuffle(encoded[offset:offset + length], width))
            offset += length
        if mode == 3:
            blocks = [xor_words(blocks[0], inverse=True), xor_words(blocks[1], inverse=True),
                      xor_words(blocks[2], 1, inverse=True), xor_words(blocks[3], 1, inverse=True), blocks[4]]
        elif mode == 4:
            blocks = [xor_words(blocks[0], inverse=True), xor_words(blocks[1], inverse=True),
                      delta_indices(blocks[2], inverse=True), delta_indices(blocks[3], inverse=True), blocks[4]]
        vertices, normals, vertex_bytes, normal_bytes, attributes = blocks
        vertex_refs, normal_refs = uints(vertex_bytes), uints(normal_bytes)
        output = bytearray(stl_header)
        for face in range(count):
            normal = normal_refs[face]
            if normal >= normal_count:
                raise ValueError("Normal index out of bounds")
            output.extend(normals[normal * 12:normal * 12 + 12])
            for index in vertex_refs[face * 3:face * 3 + 3]:
                if index >= vertex_count:
                    raise ValueError("Vertex index out of bounds")
                output.extend(vertices[index * 12:index * 12 + 12])
            output.extend(attributes[face * 2:face * 2 + 2])
        result = bytes(output)
    if hashlib.sha256(result).digest() != digest:
        raise ValueError("Reconstructed SHA-256 mismatch")
    return result
