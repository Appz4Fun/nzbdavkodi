"""Newznab stub: serves caps + canned search results + NZB fixture files."""

import os
from flask import Flask, request, Response

app = Flask(__name__)
FIXTURE_DIR = "/fixtures"

_CAPS_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server version="1.0" title="Mock Indexer" strapline="E2E Harness"/>
  <limits max="100" default="25"/>
  <registration available="no" open="no"/>
  <searching>
    <search available="yes" supportedParams="q"/>
    <tv-search available="yes" supportedParams="q,season,ep"/>
    <movie-search available="yes" supportedParams="q,imdbid"/>
  </searching>
  <categories>
    <category id="2000" name="Movies">
      <subcat id="2040" name="Movies/HD"/>
    </category>
  </categories>
</caps>"""

# Two candidates with overlapping article IDs so peer validation passes.
# Both enclosure URLs point back to this service so the orchestrator can
# download the NZB bytes for article-ID extraction.
_SEARCH_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/"
     xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>Mock Indexer Search</title>
    <link>http://mock-indexer:5076/</link>
    <item>
      <title>Sample.Movie.2024.1080p.BluRay.x264-GROUP1</title>
      <guid isPermaLink="true">http://mock-indexer:5076/nzb/primary.nzb</guid>
      <link>http://mock-indexer:5076/nzb/primary.nzb</link>
      <enclosure url="http://mock-indexer:5076/nzb/primary.nzb"
                 length="2250000" type="application/x-nzb"/>
      <newznab:attr name="category" value="2040"/>
      <newznab:attr name="size" value="2250000"/>
      <pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate>
    </item>
    <item>
      <title>Sample.Movie.2024.1080p.BluRay.x264-GROUP2</title>
      <guid isPermaLink="true">http://mock-indexer:5076/nzb/peer.nzb</guid>
      <link>http://mock-indexer:5076/nzb/peer.nzb</link>
      <enclosure url="http://mock-indexer:5076/nzb/peer.nzb"
                 length="2250000" type="application/x-nzb"/>
      <newznab:attr name="category" value="2040"/>
      <newznab:attr name="size" value="2250000"/>
      <pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>"""


@app.route("/api")
def api():
    t = request.args.get("t", "")
    if t == "caps":
        return Response(_CAPS_XML, mimetype="application/xml")
    return Response(_SEARCH_XML, mimetype="application/rss+xml")


@app.route("/nzb/<name>")
def nzb(name):
    path = os.path.join(FIXTURE_DIR, name)
    if not os.path.isfile(path):
        return f"Not found: {name}", 404
    with open(path, "rb") as fh:
        return Response(fh.read(), mimetype="application/x-nzb")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5076)
