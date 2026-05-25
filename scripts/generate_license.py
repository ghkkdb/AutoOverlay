from __future__ import annotations

import argparse
import base64
import json
import re
from pathlib import Path

from cryptography.hazmat.primitives import serialization


def canonical_json(payload: dict[str, str]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def safe_filename_part(value: str) -> str:
    value = value.strip() or "user"
    return re.sub(r'[<>:"/\\|?*\s]+', "_", value).strip("._") or "user"


def generate_license_file(
    machine_code: str,
    owner: str,
    expires_at: str,
    private_key_path: Path,
    output_path: Path | None = None,
    output_dir: Path | None = None,
) -> Path:
    private_key = serialization.load_pem_private_key(private_key_path.read_bytes(), password=None)
    signed_payload = {
        "machine_code": machine_code.strip().upper(),
        "owner": owner.strip(),
        "expires_at": expires_at.strip() or "never",
    }
    signature = private_key.sign(canonical_json(signed_payload))
    license_payload = {
        **signed_payload,
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    if output_path is None:
        owner_part = safe_filename_part(owner)
        machine_part = safe_filename_part(machine_code.strip().upper())
        output_path = (output_dir or Path.cwd()) / f"license_{owner_part}_{machine_part}.lic"
    output_path.write_text(json.dumps(license_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path.resolve()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate AutoOverlay license file.")
    parser.add_argument("--machine-code", required=True, help="Target machine code shown by AutoOverlay.")
    parser.add_argument("--owner", default="AutoOverlay User", help="License owner name.")
    parser.add_argument("--expires-at", default="never", help="Expiration date in YYYY-MM-DD, or never.")
    parser.add_argument("--private-key", default="scripts/license_private.pem", help="Ed25519 private key PEM path.")
    parser.add_argument("--output", default="", help="Output license file path. Defaults to license_OWNER_MACHINE.lic.")
    args = parser.parse_args()

    output_path = generate_license_file(
        machine_code=args.machine_code,
        owner=args.owner,
        expires_at=args.expires_at,
        private_key_path=Path(args.private_key),
        output_path=Path(args.output) if args.output else None,
    )
    print(f"License written: {output_path}")


if __name__ == "__main__":
    main()
