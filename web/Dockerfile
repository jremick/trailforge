# Multi-arch (arm64 / amd64) production PWA build for TrailForge web UI.
# Build static Vite assets natively on $BUILDPLATFORM (avoids QEMU arm64 V8/esbuild deadlocks)
# while keeping the final Nginx runtime stage multi-arch for every target platform.
FROM --platform=$BUILDPLATFORM node:22-alpine AS builder

WORKDIR /app

COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY tsconfig.json vite.config.ts capacitor.config.ts index.html ./
COPY public ./public
COPY src ./src

RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --chmod=755 nginx-ingress.sh /docker-entrypoint.d/19-trailforge-ingress.sh
USER root
RUN install -d -m 700 -o 101 -g 101 /etc/nginx/trailforge-ingress \
    && install -m 600 -o 101 -g 101 /dev/null /etc/nginx/trailforge-ingress/ingress.conf
USER 101
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1

CMD ["nginx", "-g", "daemon off;"]
