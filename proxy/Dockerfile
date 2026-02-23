FROM nginx:1.27-alpine

RUN apk add --no-cache openssl

COPY start.sh /docker-entrypoint.d/05-generate-cert.sh
RUN chmod +x /docker-entrypoint.d/05-generate-cert.sh
