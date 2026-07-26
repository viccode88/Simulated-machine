FROM alpine:3.20
RUN apk add --no-cache stunnel openssl
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 802
ENTRYPOINT ["/entrypoint.sh"]
