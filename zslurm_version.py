"""Release identity shared by the manager, chief, and package metadata.

Bump VERSION and the matching setup.py package version whenever deploying
changed manager or chief code. A chief reports this value at registration
so mixed installations are visible.
"""

VERSION = "0.2.1"
