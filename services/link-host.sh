#!/usr/bin/env bash
set -euo pipefail

here=$(cd -- "$(dirname -- "$0")" && pwd -P)
config_dir=${XDG_CONFIG_HOME:-"$HOME/.config"}
shopt -s nullglob

link_files() {
    local src_root=$1 dest_root=$2 src dest
    shift 2
    for src in "$@"; do
        dest="$dest_root/${src#"$src_root"/}"
        if [[ -e "$dest" && ! -L "$dest" ]] && ! cmp -s -- "$src" "$dest"; then
            printf 'Existing local file differs: %s\n' "$dest" >&2
            return 1
        fi
    done
    for src in "$@"; do
        dest="$dest_root/${src#"$src_root"/}"
        mkdir -p -- "$(dirname -- "$dest")"
        ln -sfnT -- "$src" "$dest"
    done
}

units="$here/systemd-user"
link_files "$units" "$config_dir/systemd/user" \
    "$units"/*.service "$units"/*.timer "$units"/*.d/*.conf
link_files "$here/quadlet" "$config_dir/containers/systemd" "$here"/quadlet/*.container
systemctl --user daemon-reload
