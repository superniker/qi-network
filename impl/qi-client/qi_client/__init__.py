"""
Qi Client — Connect any AI agent to Qi Network.

Cross-platform SDK. One import, four methods:
    connect()  →  join the DHT via bootstrap
    publish()  →  announce Agent Card to registry + DHT
    search()   →  find agents by capability via registry
    find()     →  locate an agent by DID via DHT

Usage:
    from qi_client import QiClient

    client = QiClient(identity=my_identity)
    client.connect()                    # bootstrap + get peers
    client.publish(agent_card)          # registry + dht store
    results = client.search("ocr")      # search registry
    card = client.find("did:key:z...")  # dht lookup
"""

from .client import QiClient, QiConfig, ConnectResult, PublishResult

__all__ = ["QiClient", "QiConfig", "ConnectResult", "PublishResult"]
