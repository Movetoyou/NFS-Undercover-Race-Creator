import argparse
import hashlib
import os
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import time


if sys.version_info < (3, 11) or not all(
    hasattr(sqlite3.Connection, method) for method in ("deserialize", "serialize")):
    print("Error: Python 3.11+ with SQLite serialization support is required.", file=sys.stderr)
    raise SystemExit(1)

DEFAULT_SEED = Path("D:/Games/Need for Speed Undercover/GLOBAL/NFSGD.BIN")
EVENT_ID = "E675"
EVENT_KEY = 0xC4FABA94
MASK_ADDRESS = 0xD582C4
HEADER_FUNCTION = 0x7E7430
SQLITE_HEADER = b"SQLite format 3\x00"
DATABASE_CAPACITY = 0x30000

def read_header_mask(executable: Path) -> bytes:
    data = executable.read_bytes()
    if data[:2] != b"MZ":
        raise ValueError("The executable is not a Windows PE file.")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\x00\x00":
        raise ValueError("The executable has an invalid PE header.")
    sections = struct.unpack_from("<H", data, pe + 6)[0]
    optional_size = struct.unpack_from("<H", data, pe + 20)[0]
    optional = pe + 24
    if struct.unpack_from("<H", data, optional)[0] != 0x10B:
        raise ValueError("The patcher requires the verified 32-bit nfs.exe layout.")
    image_base = struct.unpack_from("<I", data, optional + 28)[0]
    section_table = optional + optional_size

    def bytes_at(address: int, length: int) -> bytes:
        relative = address - image_base
        for index in range(sections):
            section = section_table + index * 40
            _, virtual, raw_size, raw_offset = struct.unpack_from("<4I", data, section + 8)
            if virtual <= relative and relative + length <= virtual + raw_size:
                offset = raw_offset + relative - virtual
                value = data[offset:offset + length]
                if len(value) == length:
                    return value
        raise ValueError("Unsupported nfs.exe layout; the required data is unavailable.")

    expected = bytes.fromhex("8b44240453555657b9") + struct.pack("<I", MASK_ADDRESS)
    if bytes_at(HEADER_FUNCTION, len(expected)) != expected:
        raise ValueError("Unsupported nfs.exe layout; the header decoder did not match.")
    return bytes_at(MASK_ADDRESS, 16)


def transform_payload(payload: bytes, seed: bytes, decrypt: bool) -> bytes:
    output = bytearray()
    previous = seed
    for offset in range(0, len(payload), 16):
        block = payload[offset:offset + 16]
        digest = hashlib.md5(previous).digest()
        transformed = bytes(a ^ b for a, b in zip(block, digest))
        output.extend(transformed)
        previous = block if decrypt else transformed
    return bytes(output)


def decode_database(encrypted: bytes, mask: bytes) -> bytes:
    if len(encrypted) < 116 or len(encrypted) % 16:
        raise ValueError("The seed file has an invalid encrypted length.")
    if len(encrypted) - 16 > DATABASE_CAPACITY:
        raise ValueError("The database exceeds the game loader's buffer capacity.")
    seed = bytes(a ^ b for a, b in zip(encrypted[:16], mask))
    database = transform_payload(encrypted[16:], seed, True)
    if database[:16] != SQLITE_HEADER:
        raise ValueError("Decryption failed: this is not the expected NFSGD database.")
    return database


def encode_database(database: bytes, header: bytes, mask: bytes) -> bytes:
    if len(database) % 16 or len(database) > DATABASE_CAPACITY:
        raise ValueError("The patched database cannot fit the game loader's buffer.")
    seed = bytes(a ^ b for a, b in zip(header, mask))
    return header + transform_payload(database, seed, False)


def open_database(data: bytes) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(data)
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite integrity verification failed.")
        return connection
    except Exception:
        connection.close()
        raise


def prepare_patch(original: bytes, mask: bytes) -> tuple[bytes | None, tuple | None, int]:
    database = decode_database(original, mask)
    connection = open_database(database)
    try:
        before = connection.execute('SELECT Type, "Key", State FROM UserUnlocks ORDER BY "Key"').fetchall()
        existing = connection.execute(
            'SELECT Type, "Key", State FROM UserUnlocks WHERE "Key" = ?', (EVENT_KEY,)
        ).fetchone()
        if existing is not None:
            if existing[0] not in (0, 2):
                raise ValueError("E675's key already belongs to a non-Quick-Race entry; it was not changed.")
            return None, existing, len(before)
        with connection:
            connection.execute(
                'INSERT INTO UserUnlocks (Type, "Key", State) VALUES (0, ?, 0)', (EVENT_KEY,)
            )
        patched_database = connection.serialize()
    finally:
        connection.close()

    patched = encode_database(patched_database, original[:16], mask)
    verified_data = decode_database(patched, mask)
    if verified_data != patched_database:
        raise ValueError("Encryption round-trip verification failed.")
    verified = open_database(verified_data)
    try:
        after = verified.execute('SELECT Type, "Key", State FROM UserUnlocks ORDER BY "Key"').fetchall()
        if after != sorted(before + [(0, EVENT_KEY, 0)], key=lambda row: row[1]):
            raise ValueError("Unlock-row preservation verification failed.")
    finally:
        verified.close()
    return patched, None, len(before)


def apply_patch(seed: Path, original: bytes, patched: bytes) -> Path:
    if seed.read_bytes() != original:
        raise ValueError("The seed changed since the preview. Run the patcher again.")
    backup = seed.with_name(f"{seed.name}.before-{EVENT_ID}.{time.time_ns()}.bak")
    with backup.open("xb") as stream:
        stream.write(original)
        stream.flush()
        os.fsync(stream.fileno())
    if backup.read_bytes() != original:
        raise ValueError(f"Backup verification failed: {backup}")
    print(f"Backup: {backup}")

    staging = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{seed.name}.{EVENT_ID}.", suffix=".tmp", dir=seed.parent, delete=False
        ) as stream:
            staging = Path(stream.name)
            stream.write(patched)
            stream.flush()
            os.fsync(stream.fileno())
        if staging.read_bytes() != patched:
            raise ValueError("Staged-file verification failed; the original seed was not replaced.")
        if seed.read_bytes() != original:
            raise ValueError("The seed changed during patching; the original seed was not replaced.")
        os.replace(staging, seed)
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)
    if seed.read_bytes() != patched:
        raise ValueError(f"Post-write verification failed. The original backup is at {backup}")
    return backup


def main() -> int:
    parser = argparse.ArgumentParser(description="Register E675 in the NFS Undercover profile database seed.")
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED, help="Path to GLOBAL/NFSGD.BIN")
    parser.add_argument("--exe", type=Path, help="Path to the verified nfs.exe; defaults to the seed's game folder")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Verify and preview without writing any files")
    mode.add_argument("--apply", action="store_true", help="Apply the previewed patch without an interactive prompt")
    args = parser.parse_args()
    application_started = False
    backup = None

    try:
        seed = args.seed.resolve(strict=True)
        executable = args.exe or seed.parent.parent / "nfs.exe"
        mask = read_header_mask(executable)
        original = seed.read_bytes()
        patched, existing, count = prepare_patch(original, mask)
        print(f"Seed: {seed}")
        print(f"Original SHA256: {hashlib.sha256(original).hexdigest()}")
        if existing is not None:
            print(f"{EVENT_ID} is already registered: Type={existing[0]}, Key={existing[1]}, State={existing[2]}.")
            print("No files changed.")
            return 0
        print(f"Verified SQLite database: {count} -> {count + 1} UserUnlocks rows.")
        print(f"Add {EVENT_ID}: Type=0, Key={EVENT_KEY} (0x{EVENT_KEY:08X}), State=0 (locked).")
        print("Only the seed is patched. Existing profile saves are not read or changed.")
        if args.dry_run:
            print("Dry run passed. No files changed.")
            return 0
        print("Close the game and database editors before applying.")
        if not args.apply and input("Type PATCH to create a backup and apply, or press Enter to cancel: ").strip() != "PATCH":
            print("Cancelled. No files changed.")
            return 0
        application_started = True
        backup = apply_patch(seed, original, patched)
        print(f"Patched SHA256: {hashlib.sha256(patched).hexdigest()}")
        print("Patch verified. Create a NEW profile and complete E002 to unlock E675 in Quick Race.")
        print("Existing profiles will not acquire the new row from this seed change.")
        return 0
    except (OSError, ValueError, struct.error, sqlite3.Error, EOFError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if backup is not None:
            print(f"\nInterrupted after patching. Backup: {backup}")
        elif application_started:
            print("\nInterrupted during application. The seed may already be patched; check with --dry-run.")
            print("Keep any backup shown above.")
        else:
            print("\nCancelled. No seed changes were made.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
