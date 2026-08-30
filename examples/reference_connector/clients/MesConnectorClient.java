import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Duration;
import java.util.HexFormat;
import java.util.UUID;

public final class MesConnectorClient {
    private static final int MAX_BODY_BYTES = 1024 * 1024;
    private static final long MAX_DOWNLOAD_BYTES = 512L * 1024 * 1024;

    private final URI baseUri;
    private final String token;
    private final HttpClient client;

    private static boolean isLoopbackLiteral(String host) {
        if ("localhost".equalsIgnoreCase(host) || "::1".equals(host)) {
            return true;
        }
        String[] octets = host.split("\\.", -1);
        if (octets.length != 4 || !"127".equals(octets[0])) {
            return false;
        }
        for (String octet : octets) {
            if (!octet.matches("[0-9]{1,3}")) {
                return false;
            }
            int value = Integer.parseInt(octet);
            if (value > 255 || (octet.length() > 1 && octet.startsWith("0"))) {
                return false;
            }
        }
        return true;
    }

    private MesConnectorClient(String baseUrl, String token) {
        URI candidate = URI.create(baseUrl.endsWith("/") ? baseUrl : baseUrl + "/");
        if (!("http".equals(candidate.getScheme()) || "https".equals(candidate.getScheme()))
                || candidate.getHost() == null
                || candidate.getUserInfo() != null
                || candidate.getQuery() != null
                || candidate.getFragment() != null) {
            throw new IllegalArgumentException("REFERENCE_CONNECTOR_URL must be a credential-free HTTP(S) URL");
        }
        String host = candidate.getHost();
        boolean loopback = isLoopbackLiteral(host);
        if ("http".equals(candidate.getScheme()) && !loopback) {
            throw new IllegalArgumentException("non-loopback REFERENCE_CONNECTOR_URL must use HTTPS");
        }
        if (token == null || token.isBlank()) {
            throw new IllegalArgumentException("REFERENCE_CONNECTOR_INBOUND_TOKEN is required");
        }
        this.baseUri = candidate;
        this.token = token;
        this.client = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(10))
                .followRedirects(HttpClient.Redirect.NEVER)
                .build();
    }

    private String request(String method, String relativePath, String json)
            throws IOException, InterruptedException {
        URI target = baseUri.resolve(relativePath);
        if (!baseUri.getScheme().equals(target.getScheme())
                || !baseUri.getAuthority().equals(target.getAuthority())) {
            throw new IllegalArgumentException("refusing a URL outside the Connector origin");
        }
        HttpRequest.Builder builder = HttpRequest.newBuilder(target)
                .timeout(Duration.ofSeconds(20))
                .header("Accept", "application/json")
                .header("Authorization", "Bearer " + token)
                .header("X-Request-ID", UUID.randomUUID().toString());
        if (json == null) {
            builder.GET();
        } else {
            builder.header("Content-Type", "application/json")
                    .method(method, HttpRequest.BodyPublishers.ofString(json, StandardCharsets.UTF_8));
        }
        HttpResponse<InputStream> response = client.send(
                builder.build(),
                HttpResponse.BodyHandlers.ofInputStream());
        byte[] body;
        try (InputStream stream = response.body()) {
            body = stream.readNBytes(MAX_BODY_BYTES + 1);
        }
        if (body.length > MAX_BODY_BYTES) {
            throw new IOException("Connector response exceeds 1 MiB");
        }
        String value = new String(body, StandardCharsets.UTF_8);
        if (response.statusCode() >= 300) {
            throw new IOException("Connector HTTP " + response.statusCode() + ": " + value);
        }
        return value;
    }

    private Path download(String externalRunId, String outputKey, String destination)
            throws IOException, InterruptedException {
        URI target = baseUri.resolve(
                "v1/orders/" + segment(externalRunId) + "/outputs/" + segment(outputKey));
        if (!baseUri.getScheme().equals(target.getScheme())
                || !baseUri.getAuthority().equals(target.getAuthority())) {
            throw new IllegalArgumentException("refusing a URL outside the Connector origin");
        }
        HttpRequest request = HttpRequest.newBuilder(target)
                .timeout(Duration.ofSeconds(60))
                .header("Authorization", "Bearer " + token)
                .header("X-Request-ID", UUID.randomUUID().toString())
                .GET()
                .build();
        HttpResponse<InputStream> response = client.send(
                request,
                HttpResponse.BodyHandlers.ofInputStream());
        try (InputStream stream = response.body()) {
            if (response.statusCode() < 200 || response.statusCode() >= 300) {
                throw new IOException("Connector HTTP " + response.statusCode());
            }
            long expectedSize;
            try {
                expectedSize = Long.parseLong(
                        response.headers().firstValue("Content-Length").orElse(""));
            } catch (NumberFormatException error) {
                throw new IOException("Connector download has invalid Content-Length", error);
            }
            String expectedDigest = response.headers()
                    .firstValue("X-Checksum-SHA256")
                    .orElse("");
            if (expectedSize < 0
                    || expectedSize > MAX_DOWNLOAD_BYTES
                    || !expectedDigest.matches("sha256:[0-9a-f]{64}")) {
                throw new IOException("Connector download size or digest is invalid");
            }
            Path targetPath = Path.of(destination).toAbsolutePath().normalize();
            Path parent = targetPath.getParent();
            if (parent == null
                    || !Files.isDirectory(parent, LinkOption.NOFOLLOW_LINKS)
                    || Files.exists(targetPath, LinkOption.NOFOLLOW_LINKS)
                    || Files.isSymbolicLink(targetPath)) {
                throw new IOException("download destination must not exist and its parent must exist");
            }
            Path temporary = Files.createTempFile(parent, ".connector-download-", ".tmp");
            try {
                MessageDigest digest;
                try {
                    digest = MessageDigest.getInstance("SHA-256");
                } catch (NoSuchAlgorithmException error) {
                    throw new IOException("SHA-256 is unavailable", error);
                }
                long size = 0;
                try (OutputStream output = Files.newOutputStream(
                        temporary,
                        StandardOpenOption.WRITE,
                        StandardOpenOption.TRUNCATE_EXISTING)) {
                    byte[] buffer = new byte[1024 * 1024];
                    int count;
                    while ((count = stream.read(buffer)) != -1) {
                        size += count;
                        if (size > expectedSize || size > MAX_DOWNLOAD_BYTES) {
                            throw new IOException("Connector download exceeds its declared size");
                        }
                        digest.update(buffer, 0, count);
                        output.write(buffer, 0, count);
                    }
                }
                String actualDigest = "sha256:" + HexFormat.of().formatHex(digest.digest());
                if (size != expectedSize || !actualDigest.equals(expectedDigest)) {
                    throw new IOException("Connector download size or SHA-256 verification failed");
                }
                Files.createLink(targetPath, temporary);
                Files.delete(temporary);
                temporary = null;
                return targetPath;
            } finally {
                if (temporary != null) {
                    Files.deleteIfExists(temporary);
                }
            }
        }
    }

    private static String segment(String value) {
        return URLEncoder.encode(value, StandardCharsets.UTF_8).replace("+", "%20");
    }

    private static String readOrder(String file) throws IOException {
        Path path = Path.of(file);
        if (!Files.isRegularFile(path, LinkOption.NOFOLLOW_LINKS)
                || Files.isSymbolicLink(path)
                || Files.size(path) > MAX_BODY_BYTES) {
            throw new IOException("order must be a regular JSON file smaller than 1 MiB");
        }
        return Files.readString(path, StandardCharsets.UTF_8);
    }

    public static void main(String[] args) throws Exception {
        boolean download = args.length == 4 && "download".equals(args[0]);
        if (args.length != 2 && !download) {
            System.err.println("usage: MesConnectorClient submit <order.json> | status|reconcile|results <external_run_id> | download <external_run_id> <output_key> <destination>");
            System.exit(64);
        }
        String baseUrl = System.getenv().getOrDefault(
                "REFERENCE_CONNECTOR_URL", "http://127.0.0.1:8090");
        MesConnectorClient connector = new MesConnectorClient(
                baseUrl,
                System.getenv("REFERENCE_CONNECTOR_INBOUND_TOKEN"));
        String result;
        if (download) {
            result = "{\"path\":\""
                    + connector.download(args[1], args[2], args[3]).toString().replace("\\", "\\\\").replace("\"", "\\\"")
                    + "\"}";
        } else if ("submit".equals(args[0])) {
            result = connector.request("POST", "v1/orders", readOrder(args[1]));
        } else {
            String suffix = switch (args[0]) {
                case "status" -> "";
                case "reconcile" -> "/reconcile";
                case "results" -> "/results";
                default -> throw new IllegalArgumentException("unsupported command: " + args[0]);
            };
            boolean readOnly = "status".equals(args[0]);
            result = connector.request(
                    readOnly ? "GET" : "POST",
                    "v1/orders/" + segment(args[1]) + suffix,
                    readOnly ? null : "{}");
        }
        System.out.println(result);
    }
}
