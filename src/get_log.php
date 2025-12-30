<?php
$log_file = "/var/log/plex_to_cache.log";
if (file_exists($log_file)) {
    echo shell_exec("tail -n 200 " . escapeshellarg($log_file));
} else {
    echo "Log file not found. Service might be starting...";
}
?>