"""Exercise real kernel pages across reopened descriptors on macOS and Linux."""
import errno
import os
from pathlib import Path
import struct
import tempfile
import unittest

import bounded_directory as directory


class DirectoryTest(unittest.TestCase):
    def test_reopened_pages_cover_large_directory_once_without_whole_scan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            expected = {f'{i:064x}.json' for i in range(701)}
            # Include long/wide names and undecodable names where the actual
            # filesystem permits them (APFS rejects invalid UTF-8).
            expected.update({'文' * 70, 'x' * 250})
            for name in expected:
                (root / name).touch()
            undecodable = os.fsdecode(b'non-utf8-\xff')
            try:
                (root / undecodable).touch()
            except OSError as exc:
                if exc.errno != errno.EILSEQ:
                    raise
            else:
                expected.add(undecodable)
            seen = []
            offset = 0
            pages = 0
            for _ in range(100):
                fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    names, following = directory.read_page(fd, offset)
                finally:
                    os.close(fd)
                self.assertLessEqual(len(names), directory.MAX_PAGE_ENTRIES)
                if not names:
                    break
                self.assertNotEqual(offset, following)
                seen.extend(name for name in names if name not in ('.', '..'))
                offset = following
                pages += 1
            else:
                self.fail('directory did not reach EOF within bounded pages')
            self.assertGreater(pages, 2)
            self.assertEqual(len(seen), len(expected))
            self.assertEqual(set(seen), expected)

    def test_native_record_parsers_refuse_corruption_and_paths(self):
        for darwin in (True, False):
            start = 21 if darwin else 19
            raw = bytearray(32)
            struct.pack_into('=H', raw, 16, len(raw))
            if darwin:
                struct.pack_into('=H', raw, 18, 2)
            raw[start:start + 3] = b'ok\0'
            self.assertEqual(directory._names(bytes(raw), darwin), ['ok'])
            undecodable = bytearray(raw)
            undecodable[start] = 255
            self.assertEqual(directory._names(bytes(undecodable), darwin),
                             [os.fsdecode(b'\xffk')])
            malformed = [bytes(raw[:23])]
            short = bytearray(raw)
            struct.pack_into('=H', short, 16, 12)
            malformed.append(bytes(short))
            path = bytearray(raw)
            path[start] = ord('/')
            malformed.append(bytes(path))
            no_end = bytearray(raw)
            no_end[start + 2:] = b'x' * (len(raw) - start - 2)
            malformed.append(bytes(no_end))
            for sample in malformed:
                with self.subTest(darwin=darwin, sample=sample):
                    with self.assertRaises(OSError) as raised:
                        directory._names(sample, darwin)
                    self.assertEqual(raised.exception.errno, errno.EIO)


if __name__ == '__main__':
    unittest.main()
