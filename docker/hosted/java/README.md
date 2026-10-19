# Java exploitation jars (not committed)

Place these before building the hosted image:

- `ysoserial-all.jar` — https://github.com/frohoff/ysoserial/releases (v0.0.6)
- `marshalsec-all.jar` — build from https://github.com/mbechler/marshalsec
  (`mvn -DskipTests package`, target/marshalsec-*-all.jar)

The image installs OpenJDK 21 and wraps both jars:
`ysoserial <gadget> '<cmd>'`, `marshalsec marshalsec.jndi.LDAPRefServer ...`.
