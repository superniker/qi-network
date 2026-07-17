"""
Qi Bootstrap — Entry Point

Usage:
  python -m qi_bootstrap.main
  QI_BOOTSTRAP_PORT=8733 python -m qi_bootstrap.main
  QI_BOOTSTRAP_DID="did:key:z..." python -m qi_bootstrap.main
"""

from qi_bootstrap.server import main

if __name__ == "__main__":
    main()
