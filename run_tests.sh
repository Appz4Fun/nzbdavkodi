#!/bin/bash
PYTHONPATH=.:repo/plugin.video.nzbdav pytest tests/ -v --tb=short -m "not integration and not functional and not extreme"
