"""
Qi Bootstrap — Kademlia DHT entry node.

A lightweight bootstrap node that helps new Qi agents join the DHT network.
Inspired by BitTorrent Mainline DHT bootstrap nodes (router.bittorrent.com).

Role:
  - The first hop for any new node joining the network
  - Maintains a Kademlia routing table of recently-seen peers
  - Responds to find_node queries to help nodes populate their own routing tables
  - Does NOT store Agent Cards or any application data

Architecture:
  契寻层 (Discovery Layer): DHT entry point
  HTTP API for bootstrap handshake, in-memory routing table, no persistence.
"""
