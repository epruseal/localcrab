FROM node:20-alpine AS deps

RUN apk add --no-cache libc6-compat
WORKDIR /app

COPY apps/web/package.json ./
RUN npm install

FROM node:20-alpine AS builder

RUN apk add --no-cache libc6-compat
WORKDIR /app

COPY --from=deps /app/node_modules ./node_modules
COPY apps/web ./

ENV NEXT_TELEMETRY_DISABLED=1

# NEXT_PUBLIC_* vars are inlined into the browser bundle by `next build`, so
# the API origin must be supplied as a build arg here -- a runtime `environment:`
# entry on the container has no effect on already-built JS (#149 design 2).
#
# (a) Changing this value requires rebuilding the web image; it does not
#     take effect by changing a runtime env var on the running container.
# (b) The default below only makes sense for a local docker-compose bring-up
#     where the browser and the compose host are the same machine. A remote
#     deployment MUST pass its own reachable API address as this build arg.
ARG NEXT_PUBLIC_API_URL=http://localhost:8001
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL

RUN npm run build

FROM node:20-alpine AS runner

WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
ENV HOSTNAME=0.0.0.0

COPY --from=builder /app/public ./public
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static

EXPOSE 3000

CMD ["node", "server.js"]
