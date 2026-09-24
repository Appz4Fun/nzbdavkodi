# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please report it responsibly:

1. **Do not** open a public issue
2. Email the maintainer or use [GitHub's private vulnerability reporting](https://github.com/Appz4Fun/nzbdavkodi/security/advisories/new)
3. Include steps to reproduce and any relevant details

We will respond within 72 hours and work to release a fix promptly.

## Supported Versions

| Version | Supported |
|---------|-----------|
| Latest release | Yes |
| Older releases | No |

## Scope

This addon runs locally on your Kodi device and communicates only with services you configure: your search provider (NZBHydra2, Prowlarr, or direct Newznab indexers) and your backend (nzbdav, InfiniDysk, or NZBGet). Security concerns include:

- API key handling and storage
- WebDAV, NZBGet, and SMB (Windows/Samba file sharing) credential management
- URL construction and validation
- XML parsing of search-provider and WebDAV responses (see `resources/lib/xml_safety.py`)
