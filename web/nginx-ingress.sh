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

case "$socket_enabled" in
    true)
        [ -d "$socket_dir" ] && [ -w "$socket_dir" ] ||
            fail "the proxy socket directory must be mounted and writable by the web service"
        case "$(stat -c '%a' "$socket_dir")" in
            *0) ;;
            *) fail "the proxy socket directory must deny access to other users" ;;
        esac
        ;;
    false) ;;
    *) fail "TRAILFORGE_NGINX_PROXY_SOCKET must be true or false" ;;
esac

proxy_mask=
case "$proxy_ip" in
    "") ;;
    *:*)
        case "$proxy_ip" in
            *[!0-9a-fA-F:.]*) fail "trusted proxy must be a literal IPv6 address" ;;
        esac
        proxy_mask=128
        ;;
    *.*.*.*)
        case "$proxy_ip" in
            *[!0-9.]*) fail "trusted proxy must be a literal IPv4 address" ;;
        esac
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
