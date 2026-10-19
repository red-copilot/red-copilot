#!/bin/sh
# ysoserial 0.0.6 predates the JDK 17+ module restrictions; run it on 11.
JAVA_BIN=/usr/lib/jvm/java-11-openjdk-amd64/bin/java
[ -x "$JAVA_BIN" ] || JAVA_BIN=java
exec "$JAVA_BIN" \
  --add-opens java.base/java.lang=ALL-UNNAMED \
  --add-opens java.base/java.util=ALL-UNNAMED \
  --add-opens java.base/java.lang.reflect=ALL-UNNAMED \
  --add-opens java.base/java.io=ALL-UNNAMED \
  --add-opens java.base/java.net=ALL-UNNAMED \
  -jar /opt/java/ysoserial-all.jar "$@"
