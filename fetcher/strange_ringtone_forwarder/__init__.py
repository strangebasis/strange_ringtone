"""strange_ringtone forwarder: the fetcher image's only way out, a CONNECT gate to YouTube (docs/PLAN.md 6.1).

A second command of the fetcher image (python -I /app/fetcher/forwarder.py,
port 3128). It allows CONNECT on port 443 to youtube.com, its subdomains and
the subdomains of googlevideo.com, decides everything it can before any DNS
lookup, resolves IPv4 only, connects only to global unicast addresses it
checked, and relays the tunnel within fixed byte, time and tunnel budgets.
Stdlib only, and it imports nothing from strange_ringtone or
strange_ringtone_fetch, so its process holds no code it doesn't need.
"""
