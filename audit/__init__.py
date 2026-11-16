"""Drip-pricing audits: Stagehand workflows that walk real purchase flows and compare advertised vs checkout prices."""

import os

from dotenv import load_dotenv

# The local browser is driven over a loopback CDP connection; keep it off any corporate proxy.
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
load_dotenv()
