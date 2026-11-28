#!/bin/sh
set -eu

config=/etc/nginx/trailforge-ingress/ingress.conf
socket_enabled=${TRAILFORGE_NGINX_PROXY_SOCKET:-false}
socket_dir=/run/trailforge-ingress
proxy_ip=${TRAILFORGE_NGINX_TRUSTED_PROXY_IP:-}

fail() {
    echo "TrailForge proxy settings: $1" >&2
    exit 1
}

is_valid_ipv4() {
    ip=$1
    case "$ip" in
        ""|.*|*.|*..*|*[!0-9.]*) return 1 ;;
    esac
    old_ifs=$IFS
    IFS=.
    set -f
    # shellcheck disable=SC2086
    set -- $ip
    set +f
    IFS=$old_ifs
    [ "$#" -eq 4 ] || return 1
    for octet in "$@"; do
        case "$octet" in
            ""|*[!0-9]*|0?*) return 1 ;;
        esac
        [ "$octet" -le 255 ] || return 1
    done
    return 0
}

count_ipv6_side() {
    side=$1
    allow_v4=$2
    if [ -z "$side" ]; then
        printf '0'
        return 0
    fi
    case "$side" in
        :*|*:*::*|*:|*::*) return 1 ;;
    esac
    old_ifs=$IFS
    IFS=:
    set -f
    # shellcheck disable=SC2086
    set -- $side
    set +f
    IFS=$old_ifs
    count=0
    total=$#
    idx=0
    for group in "$@"; do
        idx=$((idx + 1))
        case "$group" in
            *.*)
                [ "$allow_v4" = 1 ] && [ "$idx" -eq "$total" ] || return 1
                is_valid_ipv4 "$group" || return 1
                count=$((count + 2))
                ;;
            ""|*[!0-9a-fA-F]*)
                return 1
                ;;
            *)
                [ "${#group}" -le 4 ] || return 1
                count=$((count + 1))
                ;;
        esac
    done
    printf '%s' "$count"
    return 0
}

is_valid_ipv6() {
    ip=$1
    case "$ip" in
        ""|*[!0-9a-fA-F:.]*|*:::*) return 1 ;;
        *::*::*) return 1 ;;
        :*)
            case "$ip" in
                ::*) ;;
                *) return 1 ;;
            esac
            ;;
    esac
    case "$ip" in
        *:)
            case "$ip" in
                *::) ;;
                *) return 1 ;;
            esac
            ;;
    esac
    case "$ip" in
        *::*)
            left=${ip%%::*}
            right=${ip#*::}
            left_count=$(count_ipv6_side "$left" 0) || return 1
            right_count=$(count_ipv6_side "$right" 1) || return 1
            [ $((left_count + right_count)) -le 7 ] || return 1
            ;;
        *)
            total_count=$(count_ipv6_side "$ip" 1) || return 1
            [ "$total_count" -eq 8 ] || return 1
            ;;
    esac
    return 0
}

case "$socket_enabled" in
    true)
        [ -d "$socket_dir" ] && [ -w "$socket_dir" ] && [ -x "$socket_dir" ] ||
            fail "the proxy socket directory must be mounted, searchable, and writable by the web service"
        case "$(stat -c '%a' "$socket_dir")" in
            *0) ;;
            *) fail "the proxy socket directory must deny access to other users" ;;
        esac
        socket_path=$socket_dir/web.sock
        if [ -S "$socket_path" ]; then
            [ -r "$socket_path" ] && [ -w "$socket_path" ] ||
                fail "cannot access existing proxy socket $socket_path"
            command -v curl >/dev/null 2>&1 ||
                fail "stale proxy socket $socket_path exists and cannot be probed without curl"
            probe_rc=0
            curl --unix-socket "$socket_path" --connect-timeout 1 --max-time 2 \
                -sS -o /dev/null http://localhost/healthz >/dev/null 2>&1 || probe_rc=$?
            if [ "$probe_rc" -eq 7 ]; then
                rm -f -- "$socket_path"
            else
                fail "proxy socket $socket_path is already in use by another process"
            fi
        elif [ -e "$socket_path" ] || [ -L "$socket_path" ]; then
            fail "proxy socket path $socket_path already exists and is not a socket"
        fi
        ;;
    false) ;;
    *) fail "TRAILFORGE_NGINX_PROXY_SOCKET must be true or false" ;;
esac

proxy_mask=
case "$proxy_ip" in
    "") ;;
    *:*)
        is_valid_ipv6 "$proxy_ip" || fail "trusted proxy must be a literal IPv6 address"
        proxy_mask=128
        ;;
    *.*)
        is_valid_ipv4 "$proxy_ip" || fail "trusted proxy must be a literal IPv4 address"
        proxy_mask=32
        ;;
    *) fail "trusted proxy must be one literal IP address" ;;
esac

if [ "$socket_enabled" = true ] || [ -n "$proxy_mask" ] || [ -s "$config" ]; then
    {
        if [ "$socket_enabled" = true ]; then
            printf 'listen unix:%s/web.sock;\nset_real_ip_from unix:;\n' "$socket_dir"
        fi
        if [ -n "$proxy_mask" ]; then
            printf 'set_real_ip_from %s/%s;\n' "$proxy_ip" "$proxy_mask"
        fi
    } > "$config"
fi
