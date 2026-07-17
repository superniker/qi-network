"""
Qi Protocol — Identity Module

Generates Ed25519 keypairs and DID:key identifiers.
"""

import base64
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import hashes
from cryptography.exceptions import InvalidSignature


# Multicodec prefix for ed25519-pub (varint encoded: 0xed01 → 0xed, 0x01)
ED25519_MULTICODEC_PREFIX = bytes([0xed, 0x01])

# Base58 BTC alphabet (Bitcoin-style, same as IPFS/DID:key)
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58btc_encode(data: bytes) -> str:
    """Encode bytes to base58btc string."""
    n = int.from_bytes(data, "big")
    result = []
    while n > 0:
        n, rem = divmod(n, 58)
        result.append(BASE58_ALPHABET[rem])
    # Handle leading zeros
    for byte in data:
        if byte == 0:
            result.append(BASE58_ALPHABET[0])
        else:
            break
    return "".join(reversed(result))


def _base58btc_decode(s: str) -> bytes:
    """Decode base58btc string to bytes."""
    n = 0
    for char in s:
        n = n * 58 + BASE58_ALPHABET.index(char)
    # Calculate byte length
    byte_len = (n.bit_length() + 7) // 8
    result = n.to_bytes(byte_len, "big")
    # Restore leading zeros
    padding = 0
    for char in s:
        if char == BASE58_ALPHABET[0]:
            padding += 1
        else:
            break
    return b"\x00" * padding + result


class QiIdentity:
    """A Qi Node's cryptographic identity."""

    def __init__(self, private_bytes: bytes | None = None):
        if private_bytes is not None:
            self._private_key = ed25519.Ed25519PrivateKey.from_private_bytes(private_bytes)
        else:
            self._private_key = ed25519.Ed25519PrivateKey.generate()
        self._public_key = self._private_key.public_key()

    @classmethod
    def generate(cls) -> "QiIdentity":
        """Generate a new random identity."""
        return cls()

    @classmethod
    def from_seed(cls, seed: bytes) -> "QiIdentity":
        """Create identity from a 32-byte seed."""
        if len(seed) != 32:
            raise ValueError(f"Seed must be 32 bytes, got {len(seed)}")
        return cls(private_bytes=seed)

    @property
    def node_id(self) -> str:
        """DID:key identifier for this node."""
        pubkey_bytes = self.public_key_bytes
        multicodec = ED25519_MULTICODEC_PREFIX + pubkey_bytes
        encoded = _base58btc_encode(multicodec)
        return f"did:key:z{encoded}"

    @property
    def public_key_bytes(self) -> bytes:
        """Raw 32-byte Ed25519 public key."""
        return self._public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    @property
    def private_key_bytes(self) -> bytes:
        """Raw 32-byte Ed25519 private key (seed)."""
        return self._private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def sign(self, message: bytes) -> bytes:
        """Sign a message with the private key, return 64-byte signature."""
        return self._private_key.sign(message)

    def verify(self, message: bytes, signature: bytes, public_key_bytes: bytes) -> bool:
        """Verify a signature against a public key."""
        try:
            pubkey = ed25519.Ed25519PublicKey.from_public_bytes(public_key_bytes)
            pubkey.verify(signature, message)
            return True
        except InvalidSignature:
            return False

    def sign_json(self, payload: dict) -> str:
        """Sign a canonical JSON payload, return base64url signature."""
        import json
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        sig = self.sign(canonical)
        return base64.urlsafe_b64encode(sig).rstrip(b"=").decode()

    @staticmethod
    def verify_json(payload: dict, signature_b64url: str, public_key_bytes: bytes) -> bool:
        """Verify a canonical JSON signature."""
        import json
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        sig = base64.urlsafe_b64decode(signature_b64url + "=" * (4 - len(signature_b64url) % 4))
        try:
            pubkey = ed25519.Ed25519PublicKey.from_public_bytes(public_key_bytes)
            pubkey.verify(sig, canonical)
            return True
        except InvalidSignature:
            return False

    @staticmethod
    def node_id_from_public_key(public_key_bytes: bytes) -> str:
        """Derive a DID:key from a raw Ed25519 public key."""
        multicodec = ED25519_MULTICODEC_PREFIX + public_key_bytes
        encoded = _base58btc_encode(multicodec)
        return f"did:key:z{encoded}"

    @staticmethod
    def public_key_from_node_id(node_id: str) -> bytes:
        """Extract raw public key from a DID:key identifier."""
        if not node_id.startswith("did:key:z"):
            raise ValueError(f"Invalid DID:key format: {node_id}")
        encoded = node_id[len("did:key:z"):]
        multicodec = _base58btc_decode(encoded)
        if multicodec[:2] != ED25519_MULTICODEC_PREFIX:
            raise ValueError(f"Unsupported multicodec prefix: {multicodec[:2].hex()}")
        return multicodec[2:]
