"""Premiere Deck: host application for Adobe Premiere Pro with a thin CEP panel.

The version below is the only version in the project. The build writes it into the panel's VERSION file and
manifest; the host compares the installed panel against it (docs/ARCHITECTURE.md, section 7).
"""

__version__ = "1.1.0"

# Protocol version of the host <-> panel messages (docs/PROTOCOL.md, section 3). Changes only when a message shape
# changes incompatibly.
PROTOCOL_VERSION = 1

APP_NAME = "Premiere Deck"
EXTENSION_BUNDLE_ID = "com.softoblivion.premieredeck"
EXTENSION_AGENT_ID = EXTENSION_BUNDLE_ID + ".agent"
EXTENSION_PANEL_ID = EXTENSION_BUNDLE_ID + ".panel"
