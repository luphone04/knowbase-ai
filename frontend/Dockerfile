# The Next.js frontend: build once, run the standalone server as a non-root user.
FROM node:24-bookworm-slim AS build

ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /app

# Dependencies first so code changes don't invalidate this layer.
COPY package.json package-lock.json ./
RUN npm ci

# NEXT_PUBLIC_* values are inlined into the browser bundle here, at build time.
# They are not secrets; the Gemini key is never passed to this image.
ARG NEXT_PUBLIC_API_BASE_URL
ARG NEXT_PUBLIC_MAX_UPLOAD_MB=10
ENV NEXT_PUBLIC_API_BASE_URL=${NEXT_PUBLIC_API_BASE_URL} \
    NEXT_PUBLIC_MAX_UPLOAD_MB=${NEXT_PUBLIC_MAX_UPLOAD_MB}

COPY . .
RUN npm run build && npm run check:bundle

FROM node:24-bookworm-slim

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000
WORKDIR /app

COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static
USER node

EXPOSE 3000
CMD ["node", "server.js"]
