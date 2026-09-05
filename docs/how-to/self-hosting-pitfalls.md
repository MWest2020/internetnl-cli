# Self-hosting an Internet.nl batch instance: four traps

Everything here was hit on a real deployment of the upstream Docker batch
stack (release 1.11.3) on a single Hetzner VPS, and cost days to find. None
of it is a criticism of the upstream project — it is a good project, and the
batch documentation is genuinely useful. These are the places where following
that documentation literally still leaves you with a broken instance, so they
are written down with the exact strings you would search for.

The deployment itself is in
[deploy-instance-vps.md](deploy-instance-vps.md); the requirements and the
"is self-hosting even for you" question are in
[../reference/self-hosted.md](../reference/self-hosted.md).

## 1. `IPV4_IP_PUBLIC=127.0.0.1` kills all container egress

**Symptom.** Every container comes up healthy. Every submitted test fails
anyway. The validating resolver cannot resolve, routinator cannot fetch its
RPKI data, and nothing on the `public-internet` Docker network can reach the
internet at all. `iptables -t nat -L POSTROUTING` shows the traffic being
source-NATed to `127.0.0.1`.

**Cause.** The batch deployment documentation tells you to set the public IP
variables to loopback:

```
INTERNETNL_DOMAINNAME=example.com \
IPV4_IP_PUBLIC=127.0.0.1 \
IPV6_IP_PUBLIC=::1 \
envsubst < docker/host-dist.env > docker/host.env
```

with the explanation that this disables the connection-test DNS server, which
a batch instance does not use. That part is true. What the documentation does
not say is that the very same two variables are also used in
`docker/compose.yaml` as the NAT source addresses of the `public-internet`
network:

```yaml
      # set NAT source IPs to the configured public IPs
      com.docker.network.host_ipv4: $IPV4_IP_PUBLIC
      com.docker.network.host_ipv6: $IPV6_IP_PUBLIC
```

So one variable carries two unrelated meanings, and the value that switches
off the connection test also tells Docker to rewrite the source address of
every outbound packet to `127.0.0.1`.

**Fix.** Put the host's *real* public addresses in `docker/host.env`, exactly
as the non-batch deployment document says, and switch off the connection-test
DNS server separately by binding the `UNBOUND_PORT_*` variables to loopback.
You end up with working egress and no publicly reachable DNS listener, which
is what the batch document was trying to achieve.

**Upstream.** Worth reporting; the clean fix is to decouple the two roles of
the variable, or to change the batch document to use the real IPs plus the
`UNBOUND_PORT_*` route.

## 2. certbot in the webserver image cannot start, so certificates never renew

**Symptom.** Your Let's Encrypt certificate quietly runs to expiry. The
container's own renewal cron produces a Python traceback instead of a
renewal:

```
+ /opt/certbot/bin/certbot renew --post-hook 'nginx -s reload'
Traceback (most recent call last):
  File "/opt/certbot/bin/certbot", line 5, in <module>
    from certbot.main import main
  File "/opt/certbot/lib/python3.12/site-packages/certbot/_internal/main.py", line 20, in <module>
    import josepy as jose
  File "/opt/certbot/lib/python3.12/site-packages/josepy/__init__.py", line 41, in <module>
    from josepy.json_util import (
```

The failure is an incompatibility between the pinned `josepy` and the version
of `pyOpenSSL` in the image, around the removal of `X509Req`.

**Fix, inside the running container:**

```sh
docker exec internetnl-prod-webserver-1 /opt/certbot/bin/pip install -U certbot josepy
docker exec internetnl-prod-webserver-1 /opt/certbot/bin/certbot renew --post-hook 'nginx -s reload'
```

**This fix is ephemeral.** It lives in the container's writable layer and is
gone the moment the image is updated or the container is recreated. Put a
calendar reminder on your certificate's expiry date until upstream ships a
fixed image, and re-apply after every update.

**Upstream.** Tracked as
[internetstandards/Internet.nl#2142](https://github.com/internetstandards/Internet.nl/issues/2142).

## 3. `docker/compose.sh` needs a TTY, so plain SSH commands fail

**Symptom.** Every compose command you run over SSH from a script or a CI job
fails, while the identical command works when you are logged in
interactively.

**Cause.** The wrapper runs the compose container with a hard-coded `-ti`:

```sh
exec docker run -ti --rm --pull=never \
```

`-t` requires a TTY, and a non-interactive `ssh host 'command'` does not
allocate one.

**Fix.** Force one with `ssh -tt`:

```sh
ssh -tt deploy@your-instance 'cd /opt/Internet.nl && docker/compose.sh ... up -d'
```

Use `-tt`, not `-t`: a single `-t` is refused when stdin is not a terminal,
which is exactly the case you are trying to fix.

## 4. Recreating a Docker network strands the old containers

**Symptom.** After a compose run that recreates a network, some containers
cannot reach the others. They look fine; they are simply still attached to a
network ID that no longer exists.

**Fix.** Remove the exited containers and bring the stack back up, rather than
restarting them:

```sh
docker ps -a --filter status=exited -q | xargs -r docker rm
docker/compose.sh ... up -d
```

Restarting a stranded container reattaches it to the same stale ID; removing
and recreating it is what actually rejoins the new network.

## Before you start

Self-hosting is a maintained service, not a script. It needs a machine with a
**fixed public IPv4 address and IPv6** — not something behind NAT — and it
keeps needing attention after the first successful deployment, as trap 2
shows.

If that is more than you want to run, the
[supporter key](supporter-key.md) route gives you a credential on an instance
someone else maintains, and the
[live demo](https://mwest2020.github.io/internetnl-cli-demo/) needs no
credential at all.
