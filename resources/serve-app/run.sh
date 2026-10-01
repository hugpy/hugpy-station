#!/bin/sh
cd "$(dirname "$(readlink -f "$0")")" && exec ./node_modules/electron/dist/electron . "$@"
