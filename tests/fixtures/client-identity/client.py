"""Disposable test client: preserve duplicate headers and report HTTP results."""
import http.client
import json
import ssl
import sys
from urllib.parse import urlsplit

results = []
for request in json.loads(sys.argv[1]):
    url = urlsplit(request["url"])
    if url.scheme == "https":
        # Only the isolated smoke stack uses the known self-signed fallback cert.
        connection = http.client.HTTPSConnection(url.hostname, url.port, context=ssl._create_unverified_context(), timeout=5)
    else:
        connection = http.client.HTTPConnection(url.hostname, url.port, timeout=5)
    connection.putrequest(request.get("method", "GET"), url.path, skip_host=True)
    connection.putheader("Host", request.get("host", url.hostname))
    for key, value in request.get("headers", []):
        connection.putheader(key, value)
    connection.endheaders()
    response = connection.getresponse()
    results.append({"status": response.status, "data": json.loads(response.read()), "retry": response.getheader("Retry-After")})
    connection.close()
print(json.dumps(results))
