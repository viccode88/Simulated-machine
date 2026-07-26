#!/bin/sh
# Modbus Security 前端：TLS(802) -> 明文 Modbus TCP(502)
set -eu
CERT_DIR=/etc/stunnel/certs
mkdir -p "$CERT_DIR"
if [ ! -f "$CERT_DIR/server.pem" ]; then
  echo "產生自簽 X.509 憑證（僅供實驗室使用）"
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
    -subj "/CN=plant-modbus-tls" \
    -keyout "$CERT_DIR/server.key" -out "$CERT_DIR/server.crt" 2>/dev/null
  cat "$CERT_DIR/server.key" "$CERT_DIR/server.crt" > "$CERT_DIR/server.pem"
  chmod 600 "$CERT_DIR/server.pem"
fi
cat > /etc/stunnel/stunnel.conf <<CONF
foreground = yes
pid =
[modbus-security]
accept = 0.0.0.0:${TLS_ACCEPT_PORT:-802}
connect = ${TLS_TARGET:-boiler:502}
cert = $CERT_DIR/server.pem
CONF
exec stunnel /etc/stunnel/stunnel.conf
