import argparse
import hashlib
import os
from pathlib import Path
import re
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
MASK_ADDRESS = 0xD582C4
HEADER_FUNCTION = 0x7E7430
SQLITE_HEADER = b"SQLite format 3\x00"
DATABASE_CAPACITY = 0x30000


def validate_event_id(value: str) -> str:
    if not re.fullmatch(r"E[0-9]{3,}", value):
        raise ValueError("Event ID must be uppercase E followed by at least three digits, for example E675.")
    return value


def read_patch_command(expected_event_id: str | None = None) -> str | None:
    target = expected_event_id or "<EventID> (for example PATCH E675)"
    command = input(f"Type PATCH {target} to create a backup and apply, or press Enter to cancel: ").split()
    if not command:
        return None
    if len(command) != 2 or command[0] != "PATCH":
        raise ValueError("Invalid command. Use PATCH followed by an Event ID, for example PATCH E675.")
    event_id = validate_event_id(command[1])
    if expected_event_id is not None and event_id != expected_event_id:
        raise ValueError(f"The command does not match the preview. Use PATCH {expected_event_id}.")
    return event_id


def event_key(event_id: str) -> int:
    data = event_id.encode("ascii")
    mask = 0xFFFFFFFF

    def mix(a, b, c):
        a = (a - b - c) & mask; a ^= c >> 13
        b = (b - c - a) & mask; b ^= (a << 8) & mask
        c = (c - a - b) & mask; c ^= b >> 13
        a = (a - b - c) & mask; a ^= c >> 12
        b = (b - c - a) & mask; b ^= (a << 16) & mask
        c = (c - a - b) & mask; c ^= b >> 5
        a = (a - b - c) & mask; a ^= c >> 3
        b = (b - c - a) & mask; b ^= (a << 10) & mask
        c = (c - a - b) & mask; c ^= b >> 15
        return a & mask, b & mask, c & mask

    a = b = 0x9E3779B9
    c = 0xABCDEF00
    offset = 0
    while offset + 12 <= len(data):
        x, y, z = struct.unpack_from("<III", data, offset)
        a, b, c = mix((a + x) & mask, (b + y) & mask, (c + z) & mask)
        offset += 12
    tail = data[offset:]
    a = (a + int.from_bytes(tail[:4], "little")) & mask
    b = (b + int.from_bytes(tail[4:8], "little")) & mask
    c = (c + len(data) + (int.from_bytes(tail[8:], "little") << 8)) & mask
    return mix(a, b, c)[2]


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


def prepare_patch(original: bytes, mask: bytes, event_id: str) -> tuple[bytes | None, tuple | None, int]:
    key = event_key(event_id)
    database = decode_database(original, mask)
    connection = open_database(database)
    try:
        before = connection.execute('SELECT Type, "Key", State FROM UserUnlocks ORDER BY "Key"').fetchall()
        existing = connection.execute(
            'SELECT Type, "Key", State FROM UserUnlocks WHERE "Key" = ?', (key,)
        ).fetchone()
        if existing is not None:
            if existing[0] not in (0, 2):
                raise ValueError(f"{event_id}'s key already belongs to a non-Quick-Race entry; it was not changed.")
            return None, existing, len(before)
        with connection:
            connection.execute(
                'INSERT INTO UserUnlocks (Type, "Key", State) VALUES (0, ?, 0)', (key,)
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
        if after != sorted(before + [(0, key, 0)], key=lambda row: row[1]):
            raise ValueError("Unlock-row preservation verification failed.")
    finally:
        verified.close()
    return patched, None, len(before)


def apply_patch(seed: Path, original: bytes, patched: bytes, event_id: str) -> Path:
    if seed.read_bytes() != original:
        raise ValueError("The seed changed since the preview. Run the patcher again.")
    label = event_id if len(event_id) <= 32 else f"{event_id[:32]}-{event_key(event_id):08X}"
    backup = seed.with_name(f"{seed.name}.before-{label}.{time.time_ns()}.bak")
    backup_created = False
    try:
        with backup.open("xb") as stream:
            backup_created = True
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
        if backup.read_bytes() != original:
            raise ValueError(f"Backup verification failed: {backup}")
    except BaseException:
        if backup_created:
            backup.unlink(missing_ok=True)
        raise
    print(f"Backup: {backup}")

    staging = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{seed.name}.{label}.", suffix=".tmp", dir=seed.parent, delete=False
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
    parser = argparse.ArgumentParser(description="Register a chosen event in the NFS Undercover profile database seed.")
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED, help="Path to GLOBAL/NFSGD.BIN")
    parser.add_argument("--exe", type=Path, help="Path to the verified nfs.exe; defaults to the seed's game folder")
    parser.add_argument("--event-id", type=validate_event_id, help="Event to preview, for example E675; required for --dry-run or --apply")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Verify and preview without writing any files")
    mode.add_argument("--apply", action="store_true", help="Apply the selected event patch without an interactive prompt")
    args = parser.parse_args()
    if (args.dry_run or args.apply) and args.event_id is None:
        parser.error("--dry-run and --apply require --event-id, for example --event-id E675")
    application_started = False
    backup = None

    try:
        seed = args.seed.resolve(strict=True)
        executable = args.exe or seed.parent.parent / "nfs.exe"
        mask = read_header_mask(executable)
        original = seed.read_bytes()
        print(f"Seed: {seed}")
        print(f"Original SHA256: {hashlib.sha256(original).hexdigest()}")
        print("Only the seed is patched. Existing profile saves are not read or changed.")
        if not args.dry_run:
            print("Close the game and database editors before applying.")
        event_id = args.event_id
        if event_id is None:
            event_id = read_patch_command()
            if event_id is None:
                print("Cancelled. No files changed.")
                return 0
        key = event_key(event_id)
        patched, existing, count = prepare_patch(original, mask, event_id)
        if existing is not None:
            print(f"Unlock key for {event_id} already exists: Type={existing[0]}, Key={existing[1]}, State={existing[2]}.")
            print("No files changed.")
            return 0
        print(f"Verified SQLite database: {count} -> {count + 1} UserUnlocks rows.")
        print(f"Add {event_id}: Type=0, Key={key} (0x{key:08X}), State=0 (locked).")
        if args.dry_run:
            print("Dry run passed. No files changed.")
            return 0
        if args.event_id is not None and not args.apply and read_patch_command(event_id) is None:
            print("Cancelled. No files changed.")
            return 0
        application_started = True
        backup = apply_patch(seed, original, patched, event_id)
        print(f"Patched SHA256: {hashlib.sha256(patched).hexdigest()}")
        print(f"Patch verified for {event_id}. Create a NEW profile and satisfy its gameplay unlock condition.")
        print("For an event in the normal Quick Race unlock list, complete E002.")
        print("Existing profiles will not acquire the new row from this seed change.")
        return 0
    except (OSError, ValueError, struct.error, sqlite3.Error, EOFError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if backup is not None:
            print(f"\nInterrupted after patching. Backup: {backup}")
        elif application_started:
            print(f"\nInterrupted during application. The seed may already be patched; check with --event-id {event_id} --dry-run.")
            print("Keep any backup shown above.")
        else:
            print("\nCancelled. No seed changes were made.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
