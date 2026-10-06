FROM nginx:stable-alpine AS runtime

# CI uses --pull and disables this stage's cache on every security-gated build.
RUN apk upgrade --no-cache && apk add --no-cache openssl
