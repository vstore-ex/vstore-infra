import json
import os
import subprocess
import unittest

class TestEdgeGatewayConfig(unittest.TestCase):
    def setUp(self):
        self.infra_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.compose_file = os.path.join(self.infra_dir, "docker-compose.yml")

    def test_compose_config_has_proxy_service_and_no_public_app_ports(self):
        """
        Tracer bullet: The edge proxy service must be defined in docker-compose.yml,
        listening on ports 80 and 443, and application services (frontend, admin)
        must publish deployment health ports only on the host loopback interface.
        """
        with open(self.compose_file, "r", encoding="utf-8") as f:
            content = f.read()

        # Edge proxy service must be defined
        self.assertIn("proxy:", content, "Edge proxy service 'proxy:' must be defined in docker-compose.yml")
        self.assertIn('"80:80"', content, "Port 80 must be mapped for the proxy")
        self.assertIn('"443:443"', content, "Port 443 must be mapped for the proxy")

        # Inspect resolved bindings so an omitted host IP cannot expose app ports publicly.
        result = subprocess.run(
            ["docker", "compose", "--env-file", ".env.example", "config", "--format", "json"],
            cwd=self.infra_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        services = json.loads(result.stdout)["services"]
        for service, port in (("frontend", "3000"), ("admin", "3001")):
            with self.subTest(service=service):
                bindings = services[service].get("ports", [])
                self.assertEqual(len(bindings), 1, "Each app needs one loopback deployment health port")
                self.assertEqual(bindings[0]["host_ip"], "127.0.0.1")
                self.assertEqual(str(bindings[0]["published"]), port)
                self.assertEqual(bindings[0]["target"], 8080)

        # Proxy must re-add NET_BIND_SERVICE after cap_drop: ALL (required for ports 80/443)
        self.assertIn("NET_BIND_SERVICE", content, "Proxy must have NET_BIND_SERVICE capability for privileged port binding")

    def test_nginx_http_redirect_and_acme_challenge_template(self):
        """
        Behavior 2: The edge proxy must have an Nginx configuration template
        that listens on port 80, serves /.well-known/acme-challenge/ directly from /var/www/certbot,
        exposes /healthz, and returns a 301 redirect to https://$host$request_uri for all other traffic.
        """
        template_file = os.path.join(self.infra_dir, "config", "nginx", "templates", "default.conf.template")
        self.assertTrue(os.path.exists(template_file), f"Template file {template_file} must exist")

        with open(template_file, "r", encoding="utf-8") as f:
            conf = f.read()

        self.assertIn("listen 80", conf, "Nginx must listen on port 80")
        self.assertIn("/.well-known/acme-challenge/", conf, "Nginx must have a location block for ACME challenge")
        self.assertIn("/var/www/certbot", conf, "ACME challenge root must point to /var/www/certbot")
        self.assertIn("return 301 https://$host$request_uri;", conf, "Nginx must return 301 redirect to HTTPS on port 80")
        self.assertIn("/healthz", conf, "Nginx must have /healthz endpoint on port 80 for container healthcheck")

    def test_nginx_https_virtual_hosts_and_proxy_pass(self):
        """
        Behavior 3: The edge proxy must configure SSL virtual hosts on port 443 for both
        ${FRONTEND_DOMAIN} and ${ADMIN_DOMAIN}.
        - Frontend host must proxy_pass to http://frontend:8080
        - Admin host must proxy_pass to http://admin:8080
        - Must include proxy headers for host, real IP, and X-Forwarded-Proto: https
        - Must load SSL certificates from /etc/letsencrypt/live/...
        """
        template_file = os.path.join(self.infra_dir, "config", "nginx", "templates", "default.conf.template")
        with open(template_file, "r", encoding="utf-8") as f:
            conf = f.read()

        self.assertIn("listen 443 ssl", conf, "Must have HTTPS listener on port 443")
        self.assertIn("${FRONTEND_DOMAIN}", conf, "Must configure storefront virtual host with ${FRONTEND_DOMAIN}")
        self.assertIn("${ADMIN_CERT_NAME}", conf, "Admin certificate can be selected independently")
        self.assertIn("${ADMIN_DOMAIN}", conf, "Must configure admin virtual host with ${ADMIN_DOMAIN}")
        self.assertIn("http://frontend:8080", conf, "Storefront must proxy to http://frontend:8080")
        self.assertIn("http://admin:8080", conf, "Admin dashboard must proxy to http://admin:8080")
        self.assertIn("proxy_set_header X-Forwarded-Proto https;", conf, "Must set X-Forwarded-Proto to https")
        self.assertIn("ssl_certificate /etc/letsencrypt/live/", conf, "Must configure ssl_certificate from letsencrypt path")
        self.assertIn("ssl_certificate_key /etc/letsencrypt/live/", conf, "Must configure ssl_certificate_key from letsencrypt path")
        self.assertIn("Strict-Transport-Security", conf, "HTTPS server blocks must include HSTS header")

    def test_env_example_defines_domains_and_compose_validates(self):
        """
        Behavior 4: .env.example must define FRONTEND_DOMAIN, ADMIN_DOMAIN, CERTBOT_EMAIL,
        and CERTBOT_STAGING so that CI and new developers have valid templates.
        Also verifies that `docker compose config --quiet` executes successfully.
        """
        env_example_file = os.path.join(self.infra_dir, ".env.example")
        with open(env_example_file, "r", encoding="utf-8") as f:
            env_content = f.read()

        self.assertIn("FRONTEND_DOMAIN=", env_content, ".env.example must define FRONTEND_DOMAIN")
        self.assertIn("ADMIN_DOMAIN=", env_content, ".env.example must define ADMIN_DOMAIN")
        self.assertIn("CERTBOT_EMAIL=", env_content, ".env.example must define CERTBOT_EMAIL")
        self.assertIn("CERTBOT_STAGING=", env_content, ".env.example must define CERTBOT_STAGING")

        # Validate docker compose syntax
        result = subprocess.run(
            ["docker", "compose", "config", "--quiet"],
            cwd=self.infra_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        self.assertEqual(result.returncode, 0, f"docker compose config failed: {result.stderr}")

    def test_dummy_certificate_fallback_entrypoint(self):
        """
        Behavior 5: To prevent Nginx from crashing on fresh deployments when certificates
        do not yet exist, a docker-entrypoint script must exist to generate temporary self-signed
        dummy certificates into /etc/letsencrypt/live/${FRONTEND_DOMAIN}, and it must be mounted
        into /docker-entrypoint.d/ in the proxy container.
        """
        with open(self.compose_file, "r", encoding="utf-8") as f:
            compose_content = f.read()

        self.assertIn("/docker-entrypoint.d", compose_content, "Proxy service must mount entrypoint script into /docker-entrypoint.d/")

        script_path = os.path.join(self.infra_dir, "config", "nginx", "entrypoint", "10-init-dummy-certs.sh")
        self.assertTrue(os.path.exists(script_path), f"Entrypoint script {script_path} must exist")

        with open(script_path, "r", encoding="utf-8") as f:
            script_content = f.read()

        self.assertIn("openssl", script_content, "Script must use openssl to create dummy certificate")
        self.assertIn("fullchain.pem", script_content, "Script must target fullchain.pem")
        self.assertIn("privkey.pem", script_content, "Script must target privkey.pem")

    def test_proxy_service_has_envsubst_filter_to_preserve_nginx_variables(self):
        """
        Behavior 6: To ensure Nginx internal variables ($host, $request_uri, $remote_addr)
        are not erased by envsubst at container boot, NGINX_ENVSUBST_FILTER must be set
        in the proxy service environment.
        """
        with open(self.compose_file, "r", encoding="utf-8") as f:
            compose_content = f.read()

        self.assertIn("NGINX_ENVSUBST_FILTER", compose_content, "proxy service must specify NGINX_ENVSUBST_FILTER")

    def test_certbot_service_defined_in_compose(self):
        """
        Slice 2 / Issue #20: certbot service must be configured in docker-compose.yml
        sharing certbot_certs and certbot_webroot volumes with proxy.
        """
        with open(self.compose_file, "r", encoding="utf-8") as f:
            compose_content = f.read()

        self.assertIn("certbot:", compose_content, "certbot service must be defined in docker-compose.yml")
        self.assertIn("certbot/certbot", compose_content, "certbot service must use official certbot image")
        self.assertIn("certbot_certs:/etc/letsencrypt", compose_content, "certbot must mount certbot_certs volume")
        self.assertIn("certbot_webroot:/var/www/certbot", compose_content, "certbot must mount certbot_webroot volume")

    def test_init_letsencrypt_bootstrap_script(self):
        """
        Slice 2 / Issue #20: scripts/init-letsencrypt.sh must exist, be executable,
        support staging mode, validate against both FRONTEND_DOMAIN and ADMIN_DOMAIN,
        and trigger an Nginx configuration reload.
        """
        script_path = os.path.join(self.infra_dir, "scripts", "init-letsencrypt.sh")
        self.assertTrue(os.path.exists(script_path), f"Bootstrap script {script_path} must exist")
        self.assertTrue(os.access(script_path, os.X_OK), f"Bootstrap script {script_path} must be executable")

        with open(script_path, "r", encoding="utf-8") as f:
            script_content = f.read()

        self.assertIn("FRONTEND_DOMAIN", script_content, "Script must read FRONTEND_DOMAIN")
        self.assertIn("ADMIN_DOMAIN", script_content, "Script must read ADMIN_DOMAIN")
        self.assertIn("CERTBOT_EMAIL", script_content, "Script must read CERTBOT_EMAIL")
        self.assertIn("CERTBOT_STAGING", script_content, "Script must support CERTBOT_STAGING")
        self.assertIn("certonly", script_content, "Script must invoke certbot certonly")
        self.assertIn("--webroot", script_content, "Script must use webroot plugin")
        self.assertIn("nginx -s reload", script_content, "Script must signal Nginx reload")

        # Entrypoint must be exec-form (single binary), not a multi-word shell string
        self.assertIn("--entrypoint certbot certbot", script_content,
                       "Script must use exec-form entrypoint (single binary) to avoid shell-form fragility")

        # --force-renewal must NOT be unconditional (burns Let's Encrypt rate limits)
        self.assertIn("CERTBOT_FORCE_RENEWAL", script_content,
                       "Script must gate --force-renewal behind an explicit env var")

    def test_certbot_renewal_cadence_and_on_boot(self):
        """
        Slice 3 / Issue #21: Certbot container must execute renewal immediately on boot
        prior to sleeping, and the loop interval must be 1 hour to support servers
        active for only 1-2 hours per day.
        """
        with open(self.compose_file, "r", encoding="utf-8") as f:
            compose_content = f.read()

        # Extract certbot entrypoint line
        self.assertIn("certbot renew", compose_content, "Entrypoint must invoke certbot renew")
        self.assertIn("sleep 1h", compose_content, "Cadence interval must be 1 hour for intermittent uptime")
        self.assertNotIn("sleep 12h", compose_content, "12h interval is unsuitable for 1-2h daily server uptime")

    def test_nginx_periodic_reload_watchdog(self):
        """
        Slice 3 / Issue #21: A dedicated non-blocking watchdog script must exist in
        config/nginx/entrypoint/ to periodically trigger `nginx -s reload` without
        requiring Docker daemon socket exposure.
        """
        script_path = os.path.join(self.infra_dir, "config", "nginx", "entrypoint", "20-periodic-reload.sh")
        self.assertTrue(os.path.exists(script_path), f"Watchdog script {script_path} must exist")
        self.assertTrue(os.access(script_path, os.X_OK), f"Watchdog script {script_path} must be executable")

        with open(script_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("nginx -s reload", content, "Watchdog script must issue nginx -s reload")
        self.assertIn("&", content, "Watchdog must run in background subshell to prevent blocking entrypoint")

    def test_ci_workflow_and_documentation(self):
        """
        Slice 4 / Issue #22: CI workflow (.github/workflows/ci.yml) must execute gateway tests,
        and README.md must document the Edge Gateway architecture and bootstrap procedure.
        """
        ci_file = os.path.join(self.infra_dir, ".github", "workflows", "ci.yml")
        with open(ci_file, "r", encoding="utf-8") as f:
            ci_content = f.read()

        self.assertIn("test_gateway.py", ci_content, "CI workflow must run test_gateway.py")

        readme_file = os.path.join(self.infra_dir, "README.md")
        with open(readme_file, "r", encoding="utf-8") as f:
            readme_content = f.read()

        self.assertIn("proxy", readme_content, "README must document proxy service")
        self.assertIn("certbot", readme_content, "README must document certbot service")
        self.assertIn("init-letsencrypt.sh", readme_content, "README must document bootstrap script")

if __name__ == "__main__":
    unittest.main()


