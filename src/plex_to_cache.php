<?php
$ptc_plugin = "plex_to_cache";
$ptc_cfg_file = "/boot/config/plugins/$ptc_plugin/settings.cfg";
$ptc_log_file = "/var/log/plex_to_cache.log";
$ptc_pid_file = "/var/run/plex_to_cache.pid";
$ptc_tracked_file = "/boot/config/plugins/$ptc_plugin/cached_files.list";

// Defaults
$ptc_cfg = [
    "ENABLE_PLEX" => "False", "PLEX_URL" => "http://localhost:32400", "PLEX_TOKEN" => "",
    "ENABLE_EMBY" => "False", "EMBY_URL" => "http://localhost:8096", "EMBY_API_KEY" => "",
    "ENABLE_JELLYFIN" => "False", "JELLYFIN_URL" => "http://localhost:8096", "JELLYFIN_API_KEY" => "",
    "CHECK_INTERVAL" => "10", "CACHE_MAX_USAGE" => "80", "COPY_DELAY" => "30",
    "CLEANUP_MODE" => "none", "MOVIE_DELETE_DELAY" => "1800", "EPISODE_KEEP_PREVIOUS" => "2",
    "CACHE_MAX_DAYS" => "7", "EXCLUDE_DIRS" => "", "MEDIA_FILETYPES" => ".mkv .mp4 .avi",
    "ARRAY_ROOT" => "/mnt/user", "CACHE_ROOT" => "/mnt/cache", "DOCKER_MAPPINGS" => ""
];

if (file_exists($ptc_cfg_file)) {
    $ptc_loaded = parse_ini_file($ptc_cfg_file);
    if ($ptc_loaded) { $ptc_cfg = array_merge($ptc_cfg, $ptc_loaded); }
}

// AJAX: Get log
if (isset($_GET['action']) && $_GET['action'] === 'log') {
    header('Content-Type: text/plain');
    echo file_exists($ptc_log_file) ? shell_exec("tail -n 200 " . escapeshellarg($ptc_log_file)) : "Log file not found. Service might be starting...";
    exit;
}

// AJAX: Test connection (uses form values sent via POST, not saved config)
if (isset($_GET['action']) && $_GET['action'] === 'test') {
    header('Content-Type: application/json');
    $service = $_GET['service'] ?? '';
    $result = ['success' => false, 'message' => 'Unknown service'];

    // Get values from POST (current form input) instead of saved config
    $test_url = $_POST['url'] ?? '';
    $test_key = $_POST['key'] ?? '';

    if ($service === 'plex') {
        $url = rtrim($test_url, '/') . '/';
        if (empty($test_key)) {
            $result = ['success' => false, 'message' => 'No Token entered'];
        } elseif (empty($test_url)) {
            $result = ['success' => false, 'message' => 'No URL entered'];
        } else {
            $ch = curl_init($url);
            curl_setopt_array($ch, [
                CURLOPT_RETURNTRANSFER => true,
                CURLOPT_TIMEOUT => 5,
                CURLOPT_SSL_VERIFYPEER => false,
                CURLOPT_SSL_VERIFYHOST => false,
                CURLOPT_HTTPHEADER => ['X-Plex-Token: ' . $test_key, 'Accept: application/json']
            ]);
            $response = curl_exec($ch);
            $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
            $error = curl_error($ch);
            curl_close($ch);

            if ($httpCode === 200 && $response) {
                $data = json_decode($response, true);
                if (isset($data['MediaContainer'])) {
                    $name = $data['MediaContainer']['friendlyName'] ?? 'Plex Server';
                    $result = ['success' => true, 'message' => "Connected! Server: $name"];
                } else {
                    $result = ['success' => false, 'message' => 'Invalid response from Plex'];
                }
            } elseif ($httpCode === 401) {
                $result = ['success' => false, 'message' => 'Invalid Token (401)'];
            } elseif ($error) {
                $result = ['success' => false, 'message' => "Error: $error"];
            } else {
                $result = ['success' => false, 'message' => "Failed (HTTP $httpCode)"];
            }
        }
    } elseif ($service === 'emby' || $service === 'jellyfin') {
        $url = rtrim($test_url, '/') . '/System/Info';

        if (empty($test_key)) {
            $result = ['success' => false, 'message' => 'No API Key entered'];
        } elseif (empty($test_url)) {
            $result = ['success' => false, 'message' => 'No URL entered'];
        } else {
            $ch = curl_init($url);
            curl_setopt_array($ch, [
                CURLOPT_RETURNTRANSFER => true,
                CURLOPT_TIMEOUT => 5,
                CURLOPT_SSL_VERIFYPEER => false,
                CURLOPT_SSL_VERIFYHOST => false,
                CURLOPT_HTTPHEADER => ['X-Emby-Token: ' . $test_key, 'Accept: application/json']
            ]);
            $response = curl_exec($ch);
            $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
            $error = curl_error($ch);
            curl_close($ch);

            if ($httpCode === 200 && $response) {
                $data = json_decode($response, true);
                if (isset($data['ServerName'])) {
                    $result = ['success' => true, 'message' => "Connected! " . $data['ServerName']];
                } else {
                    $result = ['success' => false, 'message' => 'Invalid response'];
                }
            } elseif ($httpCode === 401) {
                $result = ['success' => false, 'message' => 'Invalid API Key (401)'];
            } elseif ($error) {
                $result = ['success' => false, 'message' => "Error: $error"];
            } else {
                $result = ['success' => false, 'message' => "Failed (HTTP $httpCode)"];
            }
        }
    }
    echo json_encode($result);
    exit;
}

// AJAX: Clear all cached media (moves plugin-cached files back to array)
if (isset($_GET['action']) && $_GET['action'] === 'clearcache') {
    header('Content-Type: application/json');
    $deleted = 0;  // Files that existed on array (deleted from cache)
    $moved = 0;    // Files moved via rsync
    $size = 0;
    $cache_root = rtrim($ptc_cfg['CACHE_ROOT'], '/');
    $array_root = '/mnt/user0';

    // Load tracked files and move them to array
    if (file_exists($ptc_tracked_file)) {
        $lines = array_filter(array_map('trim', file($ptc_tracked_file)));
        foreach ($lines as $line) {
            // Format: path|timestamp - extract just the path
            $parts = explode('|', $line);
            $file = $parts[0];

            if (file_exists($file)) {
                $file_size = filesize($file);
                $rel = str_replace($cache_root, '', $file);
                $dst = $array_root . $rel;

                // Check if file already exists on array - then we can delete the cache copy
                if (file_exists($dst)) {
                    if (@unlink($file)) {
                        $size += $file_size;
                        $deleted++;
                    }
                } else {
                    // Move file to array using rsync
                    $dst_dir = dirname($dst);
                    if (!is_dir($dst_dir)) {
                        @mkdir($dst_dir, 0777, true);
                    }
                    $cmd = "rsync -a --inplace --remove-source-files " . escapeshellarg($file) . " " . escapeshellarg($dst) . " 2>&1";
                    exec($cmd, $output, $ret);
                    if ($ret === 0) {
                        $size += $file_size;
                        $moved++;
                    }
                }
            }
        }
        // Clear the tracking file
        file_put_contents($ptc_tracked_file, '');
    }

    $size_mb = round($size / 1024 / 1024, 2);
    $total = $deleted + $moved;
    echo json_encode(['success' => true, 'message' => "Done: $total files ({$size_mb} MB). Moved: $moved, Deleted (existed on array): $deleted"]);
    exit;
}

// AJAX: Move ALL files to array (including plugin-cached media)
if (isset($_GET['action']) && $_GET['action'] === 'moveall') {
    header('Content-Type: application/json');
    $cache_root = $ptc_cfg['CACHE_ROOT'];
    $array_root = '/mnt/user0'; // Physical disks
    $count = 0;
    $size = 0;

    if (is_dir($cache_root)) {
        $iterator = new RecursiveIteratorIterator(
            new RecursiveDirectoryIterator($cache_root, RecursiveDirectoryIterator::SKIP_DOTS),
            RecursiveIteratorIterator::SELF_FIRST
        );

        $files_to_move = [];
        foreach ($iterator as $file) {
            if ($file->isFile()) {
                $files_to_move[] = $file->getPathname();
            }
        }

        foreach ($files_to_move as $src) {
            $rel = str_replace($cache_root, '', $src);
            $dst = $array_root . $rel;
            $dst_dir = dirname($dst);

            $file_size = filesize($src);

            // Check if file already exists on array - then we can delete the cache copy
            if (file_exists($dst)) {
                @unlink($src);
                $size += $file_size;
                $count++;
            } else {
                // Create destination directory if needed
                if (!is_dir($dst_dir)) {
                    @mkdir($dst_dir, 0777, true);
                }

                // Move file using rsync
                $cmd = "rsync -a --inplace --remove-source-files " . escapeshellarg($src) . " " . escapeshellarg($dst) . " 2>&1";
                exec($cmd, $output, $ret);
                if ($ret === 0) {
                    $size += $file_size;
                    $count++;
                }
            }
        }

        // Clean empty directories
        $dirs = new RecursiveIteratorIterator(
            new RecursiveDirectoryIterator($cache_root, RecursiveDirectoryIterator::SKIP_DOTS),
            RecursiveIteratorIterator::CHILD_FIRST
        );
        foreach ($dirs as $dir) {
            if ($dir->isDir()) {
                @rmdir($dir->getPathname());
            }
        }
    }

    // Clear the tracking file since all files moved
    if (file_exists($ptc_tracked_file)) {
        file_put_contents($ptc_tracked_file, '');
    }

    $size_gb = round($size / 1024 / 1024 / 1024, 2);
    echo json_encode(['success' => true, 'message' => "Moved $count files ({$size_gb} GB) to array"]);
    exit;
}

// AJAX: Move other files to array (everything EXCEPT plugin-cached media)
if (isset($_GET['action']) && $_GET['action'] === 'moveother') {
    header('Content-Type: application/json');
    $cache_root = rtrim($ptc_cfg['CACHE_ROOT'], '/');
    $array_root = '/mnt/user0';
    $count = 0;
    $size = 0;
    $skipped = 0;
    $errors = [];

    // Check if cache_root exists
    if (!is_dir($cache_root)) {
        echo json_encode(['success' => false, 'message' => "Cache root not found: $cache_root"]);
        exit;
    }

    // Load tracked files (files we want to KEEP on cache)
    // Format is: path|timestamp - we only need the path
    $tracked_paths = [];
    if (file_exists($ptc_tracked_file)) {
        $lines = array_filter(array_map('trim', file($ptc_tracked_file)));
        foreach ($lines as $line) {
            $parts = explode('|', $line);
            $tracked_paths[$parts[0]] = true;
        }
    }

    $iterator = new RecursiveIteratorIterator(
        new RecursiveDirectoryIterator($cache_root, RecursiveDirectoryIterator::SKIP_DOTS),
        RecursiveIteratorIterator::SELF_FIRST
    );

    $files_to_move = [];
    $total_files = 0;
    foreach ($iterator as $file) {
        if ($file->isFile()) {
            $total_files++;
            $path = $file->getPathname();
            // Skip if this file is tracked (plugin-cached)
            if (isset($tracked_paths[$path])) {
                $skipped++;
                continue;
            }
            $files_to_move[] = $path;
        }
    }

    foreach ($files_to_move as $src) {
        $rel = str_replace($cache_root, '', $src);
        $dst = $array_root . $rel;
        $dst_dir = dirname($dst);

        $file_size = @filesize($src);
        if ($file_size === false) continue;

        // Check if file already exists on array - then we can delete the cache copy
        if (file_exists($dst)) {
            if (@unlink($src)) {
                $size += $file_size;
                $count++;
            }
        } else {
            // Create destination directory if needed
            if (!is_dir($dst_dir)) {
                @mkdir($dst_dir, 0777, true);
            }

            // Move file using rsync
            $cmd = "rsync -a --inplace --remove-source-files " . escapeshellarg($src) . " " . escapeshellarg($dst) . " 2>&1";
            $output = [];
            exec($cmd, $output, $ret);
            if ($ret === 0) {
                $size += $file_size;
                $count++;
            } else {
                $errors[] = basename($src) . ": " . implode(" ", $output);
            }
        }
    }

    // Clean empty directories
    $dirs = new RecursiveIteratorIterator(
        new RecursiveDirectoryIterator($cache_root, RecursiveDirectoryIterator::SKIP_DOTS),
        RecursiveIteratorIterator::CHILD_FIRST
    );
    foreach ($dirs as $dir) {
        if ($dir->isDir()) {
            @rmdir($dir->getPathname());
        }
    }

    $size_gb = round($size / 1024 / 1024 / 1024, 2);
    $msg = "Moved $count files ({$size_gb} GB). Total: $total_files, Skipped (tracked): $skipped";
    if (count($errors) > 0) {
        $msg .= ". Errors: " . count($errors);
    }
    echo json_encode(['success' => true, 'message' => $msg]);
    exit;
}

// AJAX: Service control
if (isset($_GET['action']) && $_GET['action'] === 'service') {
    header('Content-Type: application/json');
    $cmd = $_GET['cmd'] ?? '';
    if (in_array($cmd, ['start', 'stop', 'restart'])) {
        shell_exec("/usr/local/emhttp/plugins/plex_to_cache/scripts/rc.plex_to_cache $cmd > /dev/null 2>&1");
        sleep(1);
        $running = file_exists($ptc_pid_file) && posix_kill((int)@file_get_contents($ptc_pid_file), 0);
        echo json_encode(['success' => true, 'running' => $running]);
    } else {
        echo json_encode(['success' => false, 'message' => 'Invalid command']);
    }
    exit;
}

// POST: Save settings
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    foreach ($ptc_cfg as $key => $val) {
        if (isset($_POST[$key])) { $ptc_cfg[$key] = $_POST[$key]; }
        else { if (strpos($key, "ENABLE_") === 0 || $key === "ENABLE_SMART_CLEANUP") { $ptc_cfg[$key] = "False"; } }
    }
    $m_str = "";
    if (isset($_POST['mapping_docker']) && isset($_POST['mapping_host'])) {
        $d_arr = $_POST['mapping_docker']; $h_arr = $_POST['mapping_host']; $pairs = [];
        for ($i=0; $i<count($d_arr); $i++) {
            if (!empty(trim($d_arr[$i])) && !empty(trim($h_arr[$i]))) { $pairs[] = trim($d_arr[$i]).":".trim($h_arr[$i]); }
        }
        $m_str = implode(";", $pairs);
    }
    $ptc_cfg['DOCKER_MAPPINGS'] = $m_str;

    $content = "";
    foreach ($ptc_cfg as $key => $val) { $content .= "$key=\"$val\"\n"; }

    if (!is_dir(dirname($ptc_cfg_file))) mkdir(dirname($ptc_cfg_file), 0777, true);
    file_put_contents($ptc_cfg_file, $content);

    shell_exec("/usr/local/emhttp/plugins/plex_to_cache/scripts/rc.plex_to_cache restart > /dev/null 2>&1 &");
    echo "<script>window.location.href = window.location.href;</script>";
    exit;
}

$mappings_pairs = [];
if (!empty($ptc_cfg['DOCKER_MAPPINGS'])) {
    $pairs = explode(";", $ptc_cfg['DOCKER_MAPPINGS']);
    foreach ($pairs as $p) { if (strpos($p, ":") !== false) { $mappings_pairs[] = explode(":", $p, 2); } }
}

// Check service status
$is_running = file_exists($ptc_pid_file) && posix_kill((int)@file_get_contents($ptc_pid_file), 0);

// Count tracked files
$tracked_count = 0;
if (file_exists($ptc_tracked_file)) {
    $tracked_count = count(array_filter(array_map('trim', file($ptc_tracked_file))));
}
?>
<style>
:root { --primary-blue: #00aaff; --bg-dark: #111; --success-green: #00cc66; --error-red: #ff4444; --warning-orange: #ff9900; }

#ptc-wrapper {
    display: grid;
    grid-template-columns: minmax(300px, 1fr) minmax(300px, 1fr) minmax(350px, 1.5fr);
    gap: 12px;
    align-items: stretch;
    width: 100%;
    box-sizing: border-box;
    padding: 10px 0;
}

@media (max-width: 1200px) {
    #ptc-wrapper {
        grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    }
}

.ptc-col {
    background: var(--bg-dark);
    border-radius: 8px;
    box-shadow: 0 0 10px rgba(0, 170, 255, 0.15);
    color: #f0f8ff;
    padding: 20px 20px 20px 20px;
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
    min-height: 650px;
}

#ptc-col-log {
    display: flex;
    flex-direction: column;
}

#ptc-log {
    background: #000;
    border: 1px solid #333;
    border-radius: 8px;
    color: #00ffaa;
    font-family: 'Courier New', monospace;
    font-size: 13px;
    padding: 15px;
    margin-top: 10px;
    white-space: pre-wrap;
    word-break: break-all;
    flex-grow: 1;
    height: 0;
    min-height: 400px;
    overflow-y: auto;
}

/* Status dot */
.status-dot {
    width: 14px;
    height: 14px;
    border-radius: 50%;
    animation: pulse 2s infinite;
    cursor: help;
    flex-shrink: 0;
}

.status-dot.running { background: var(--success-green); }
.status-dot.stopped { background: var(--error-red); animation: none; }

@keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.5; }
}

/* Top control bar with Save and Service buttons */
.top-control-bar {
    display: flex;
    gap: 10px;
    margin-bottom: 15px;
    align-items: stretch;
}

.top-control-bar input[type="submit"],
.top-control-bar button {
    padding: 10px 16px;
    font-weight: bold;
    text-transform: uppercase;
    cursor: pointer;
    font-size: 13px;
    border-radius: 4px;
}

.top-control-bar input[type="submit"] {
    flex: 1;
}

.top-control-bar .service-btn {
    background: #222;
    border: 1px solid #444;
    color: #fff;
}

.top-control-bar .service-btn:hover {
    background: #333;
    border-color: var(--primary-blue);
}

/* Form elements */
.section-header { color: var(--primary-blue); font-size: 18px; font-weight: bold; margin-bottom: 15px; margin-top: 20px; border-bottom: 1px solid #333; padding-bottom: 5px; display: flex; align-items: center; gap: 8px; }
.section-header:first-of-type { margin-top: 0; }

.form-pair { display: flex; align-items: center; margin-bottom: 12px; gap: 10px; width: 100%; }
.form-pair label { flex: 0 0 100px; color: var(--primary-blue); font-weight: bold; font-size: 14px; position: relative; cursor: help; }
.form-input-wrapper { display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1; }

.expand-row .form-input-wrapper { flex: 1; }
.expand-row input { width: 100% !important; max-width: none !important; box-sizing: border-box !important; }

/* Custom Tooltip */
.form-pair label:after {
    content: attr(data-tooltip);
    position: absolute;
    bottom: 130%;
    left: 0;
    background: #222;
    color: #fff;
    padding: 10px 14px;
    border-radius: 6px;
    font-size: 12.5px;
    font-weight: normal;
    width: 280px;
    z-index: 999;
    box-shadow: 0 5px 20px rgba(0,0,0,0.6);
    border: 1px solid var(--primary-blue);
    visibility: hidden;
    opacity: 0;
    pointer-events: none;
    white-space: normal;
    line-height: 1.5;
    text-transform: none;
}
.form-pair label:hover:after { visibility: visible; opacity: 1; transition: opacity 0.2s ease 0.5s; }

.ptc-input { background: #111 !important; border: 1px solid #444 !important; border-radius: 4px !important; color: #fff !important; padding: 6px 10px !important; font-size: 14px !important; height: 32px !important; width: 100% !important; max-width: none !important; box-sizing: border-box !important; }
.ptc-input:focus { border-color: var(--primary-blue) !important; outline: none !important; }
.input-small { width: 70px !important; flex: 0 0 70px !important; text-align: right; }
.form-input-wrapper input[type="checkbox"] { accent-color: var(--primary-blue); width: 18px; height: 18px; cursor: pointer; }
.unit-label { font-size: 12px; color: #777; white-space: nowrap; }

#mapping_table { width: 100%; border-collapse: collapse; margin-top: 10px; }
#mapping_table th { text-align: left; color: var(--primary-blue); padding: 8px; border-bottom: 1px solid #333; font-size: 13px; }
#mapping_table td { padding: 5px 0; }


/* Test button */
.btn-test {
    padding: 4px 10px;
    font-size: 11px;
    cursor: pointer;
    border: 1px solid #444;
    border-radius: 4px;
    background: #222;
    color: #fff;
    white-space: nowrap;
}
.btn-test:hover { background: #333; border-color: var(--primary-blue); }
.btn-test.success { border-color: var(--success-green); color: var(--success-green); }
.btn-test.error { border-color: var(--error-red); color: var(--error-red); }

/* Action buttons */
.btn-action {
    padding: 8px 16px;
    font-size: 12px;
    cursor: pointer;
    border-radius: 4px;
    background: transparent;
    margin-top: 10px;
    display: block;
    width: 100%;
    text-align: center;
}
.btn-action.danger {
    border: 1px solid var(--error-red);
    color: var(--error-red);
}
.btn-action.danger:hover { background: var(--error-red); color: #fff; }
.btn-action.warning {
    border: 1px solid var(--warning-orange);
    color: var(--warning-orange);
}
.btn-action.warning:hover { background: var(--warning-orange); color: #fff; }

.cache-info {
    font-size: 12px;
    color: #888;
    margin-top: 15px;
    padding: 10px;
    background: #1a1a1a;
    border-radius: 4px;
}
.cache-info strong { color: var(--primary-blue); }

/* Cleanup mode selector */
.cleanup-mode-selector {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-bottom: 15px;
}
.radio-option {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 12px;
    background: #1a1a1a;
    border: 1px solid #333;
    border-radius: 6px;
    cursor: pointer;
    transition: all 0.2s;
}
.radio-option:hover { border-color: #555; }
.radio-option.selected { border-color: var(--primary-blue); background: #1a2a3a; }
.radio-option input[type="radio"] { accent-color: var(--primary-blue); width: 16px; height: 16px; margin: 0; }
.radio-label { color: #fff; font-weight: bold; font-size: 13px; min-width: 110px; }
.radio-desc { color: #888; font-size: 12px; }
.cleanup-options { margin-top: 10px; padding: 10px; background: #0a0a0a; border-radius: 6px; border: 1px solid #222; }
</style>

<form method="post" autocomplete="off">
    <div id="ptc-wrapper">
        <div class="ptc-col" id="ptc-col-servers">
            <div class="top-control-bar">
                <input type="submit" value="Save & Apply">
                <button type="button" class="service-btn" onclick="serviceControl('start')">Start</button>
                <button type="button" class="service-btn" onclick="serviceControl('stop')">Stop</button>
                <span class="status-dot <?= $is_running ? 'running' : 'stopped' ?>" id="status-dot" title="<?= $is_running ? 'Running' : 'Stopped' ?>"></span>
            </div>

            <div class="section-header"><i class="fa fa-play-circle"></i> Plex Server</div>
            <div class="form-pair"><label data-tooltip="Enables monitoring for Plex streams.">Enable:</label><div class="form-input-wrapper"><input type="checkbox" name="ENABLE_PLEX" value="True" <?= $ptc_cfg['ENABLE_PLEX'] == 'True' ? 'checked' : '' ?> ></div></div>
            <div class="form-pair expand-row"><label data-tooltip="The web address of your Plex server.">URL:</label><div class="form-input-wrapper"><input type="text" name="PLEX_URL" value="<?= htmlspecialchars($ptc_cfg['PLEX_URL']) ?>" class="ptc-input"></div></div>
            <div class="form-pair expand-row"><label data-tooltip="Your Plex Authentication Token.">Token:</label><div class="form-input-wrapper"><input type="password" name="PLEX_TOKEN" value="<?= htmlspecialchars($ptc_cfg['PLEX_TOKEN']) ?>" class="ptc-input" onmouseover="this.type='text'" onmouseout="this.type='password'" autocomplete="new-password"><button type="button" class="btn-test" onclick="testConnection('plex', this)">Test</button></div></div>

            <div class="section-header"><i class="fa fa-server"></i> Emby Server</div>
            <div class="form-pair"><label data-tooltip="Enables monitoring for Emby streams.">Enable:</label><div class="form-input-wrapper"><input type="checkbox" name="ENABLE_EMBY" value="True" <?= $ptc_cfg['ENABLE_EMBY'] == 'True' ? 'checked' : '' ?> ></div></div>
            <div class="form-pair expand-row"><label data-tooltip="The web address of your Emby server.">URL:</label><div class="form-input-wrapper"><input type="text" name="EMBY_URL" value="<?= htmlspecialchars($ptc_cfg['EMBY_URL']) ?>" class="ptc-input"></div></div>
            <div class="form-pair expand-row"><label data-tooltip="The API key for Emby.">API Key:</label><div class="form-input-wrapper"><input type="password" name="EMBY_API_KEY" value="<?= htmlspecialchars($ptc_cfg['EMBY_API_KEY']) ?>" class="ptc-input" onmouseover="this.type='text'" onmouseout="this.type='password'" autocomplete="new-password"><button type="button" class="btn-test" onclick="testConnection('emby', this)">Test</button></div></div>

            <div class="section-header"><i class="fa fa-film"></i> Jellyfin Server</div>
            <div class="form-pair"><label data-tooltip="Enables monitoring for Jellyfin streams.">Enable:</label><div class="form-input-wrapper"><input type="checkbox" name="ENABLE_JELLYFIN" value="True" <?= $ptc_cfg['ENABLE_JELLYFIN'] == 'True' ? 'checked' : '' ?> ></div></div>
            <div class="form-pair expand-row"><label data-tooltip="The web address of your Jellyfin server.">URL:</label><div class="form-input-wrapper"><input type="text" name="JELLYFIN_URL" value="<?= htmlspecialchars($ptc_cfg['JELLYFIN_URL']) ?>" class="ptc-input"></div></div>
            <div class="form-pair expand-row"><label data-tooltip="The API key for Jellyfin.">API Key:</label><div class="form-input-wrapper"><input type="password" name="JELLYFIN_API_KEY" value="<?= htmlspecialchars($ptc_cfg['JELLYFIN_API_KEY']) ?>" class="ptc-input" onmouseover="this.type='text'" onmouseout="this.type='password'" autocomplete="new-password"><button type="button" class="btn-test" onclick="testConnection('jellyfin', this)">Test</button></div></div>
        </div>

        <div class="ptc-col" id="ptc-col-tuning">
            <div class="section-header"><i class="fa fa-folder-open"></i> Storage Paths</div>
            <div class="form-pair expand-row"><label data-tooltip="The primary path of your Unraid array.">Array Root:</label><div class="form-input-wrapper"><input type="text" name="ARRAY_ROOT" value="<?= htmlspecialchars($ptc_cfg['ARRAY_ROOT']) ?>" class="ptc-input"></div></div>
            <div class="form-pair expand-row"><label data-tooltip="The path of your cache pool.">Cache Root:</label><div class="form-input-wrapper"><input type="text" name="CACHE_ROOT" value="<?= htmlspecialchars($ptc_cfg['CACHE_ROOT']) ?>" class="ptc-input"></div></div>
            <div class="form-pair expand-row"><label data-tooltip="Folder names to be ignored (comma-separated).">Exclude:</label><div class="form-input-wrapper"><input type="text" name="EXCLUDE_DIRS" value="<?= htmlspecialchars($ptc_cfg['EXCLUDE_DIRS']) ?>" placeholder="temp,downloads" class="ptc-input"></div></div>

            <div class="section-header"><i class="fa fa-exchange"></i> Docker Mappings</div>
            <table id="mapping_table"><thead><tr><th>Host Path</th><th>Docker Path</th><th></th></tr></thead><tbody></tbody></table>
            <button type="button" onclick="addMappingRow()" style="padding: 6px 12px; font-size: 12px; margin-top: 10px; cursor: pointer;">+ Add Mapping</button>

            <div class="section-header"><i class="fa fa-cogs"></i> Tuning</div>
            <div class="form-pair"><label data-tooltip="Interval in seconds to check for active streams.">Interval:</label><div class="form-input-wrapper"><input type="number" name="CHECK_INTERVAL" value="<?= htmlspecialchars($ptc_cfg['CHECK_INTERVAL']) ?>" class="ptc-input input-small"><span class="unit-label">sec</span></div></div>
            <div class="form-pair"><label data-tooltip="Delay before starting to copy files.">Copy Delay:</label><div class="form-input-wrapper"><input type="number" name="COPY_DELAY" value="<?= htmlspecialchars($ptc_cfg['COPY_DELAY']) ?>" class="ptc-input input-small"><span class="unit-label">sec</span></div></div>
            <div class="form-pair"><label data-tooltip="Maximum cache usage percentage before stopping copies.">Max Cache:</label><div class="form-input-wrapper"><input type="number" name="CACHE_MAX_USAGE" value="<?= htmlspecialchars($ptc_cfg['CACHE_MAX_USAGE']) ?>" class="ptc-input input-small"><span class="unit-label">%</span></div></div>

            <div class="section-header"><i class="fa fa-clock-o"></i> Auto Cleanup</div>
            <div class="cleanup-mode-selector">
                <label class="radio-option <?= $ptc_cfg['CLEANUP_MODE'] == 'none' ? 'selected' : '' ?>">
                    <input type="radio" name="CLEANUP_MODE" value="none" <?= $ptc_cfg['CLEANUP_MODE'] == 'none' ? 'checked' : '' ?> onchange="updateCleanupUI()">
                    <span class="radio-label">Disabled</span>
                    <span class="radio-desc">No automatic cleanup</span>
                </label>
                <label class="radio-option <?= $ptc_cfg['CLEANUP_MODE'] == 'smart' ? 'selected' : '' ?>">
                    <input type="radio" name="CLEANUP_MODE" value="smart" <?= $ptc_cfg['CLEANUP_MODE'] == 'smart' ? 'checked' : '' ?> onchange="updateCleanupUI()">
                    <span class="radio-label">Smart Cleanup</span>
                    <span class="radio-desc">Remove watched media automatically</span>
                </label>
                <label class="radio-option <?= $ptc_cfg['CLEANUP_MODE'] == 'days' ? 'selected' : '' ?>">
                    <input type="radio" name="CLEANUP_MODE" value="days" <?= $ptc_cfg['CLEANUP_MODE'] == 'days' ? 'checked' : '' ?> onchange="updateCleanupUI()">
                    <span class="radio-label">Days-based</span>
                    <span class="radio-desc">Move files after X days</span>
                </label>
            </div>

            <div id="smart-cleanup-options" class="cleanup-options" style="display: <?= $ptc_cfg['CLEANUP_MODE'] == 'smart' ? 'block' : 'none' ?>;">
                <div class="form-pair"><label data-tooltip="Delay in seconds before deleting watched movies.">Delete Delay:</label><div class="form-input-wrapper"><input type="number" name="MOVIE_DELETE_DELAY" value="<?= htmlspecialchars($ptc_cfg['MOVIE_DELETE_DELAY']) ?>" class="ptc-input input-small"><span class="unit-label">sec</span></div></div>
                <div class="form-pair"><label data-tooltip="Number of previous episodes to keep in cache.">Keep Episodes:</label><div class="form-input-wrapper"><input type="number" name="EPISODE_KEEP_PREVIOUS" value="<?= htmlspecialchars($ptc_cfg['EPISODE_KEEP_PREVIOUS']) ?>" class="ptc-input input-small"><span class="unit-label">ep</span></div></div>
            </div>

            <div id="days-cleanup-options" class="cleanup-options" style="display: <?= $ptc_cfg['CLEANUP_MODE'] == 'days' ? 'block' : 'none' ?>;">
                <div class="form-pair"><label data-tooltip="Move cached files back to array after this many days.">Max Days:</label><div class="form-input-wrapper"><input type="number" name="CACHE_MAX_DAYS" value="<?= htmlspecialchars($ptc_cfg['CACHE_MAX_DAYS']) ?>" class="ptc-input input-small"><span class="unit-label">days</span></div></div>
            </div>

            <div class="cache-info">
                <strong>Tracked Media Files:</strong> <?= $tracked_count ?> files cached by this plugin
            </div>

            <button type="button" class="btn-action warning" onclick="moveOther()"><i class="fa fa-arrow-right"></i> Move Other Files to Array</button>
            <button type="button" class="btn-action warning" onclick="clearCache()"><i class="fa fa-arrow-right"></i> Move Cached Media to Array</button>
            <button type="button" class="btn-action danger" onclick="moveAll()"><i class="fa fa-arrow-right"></i> Move ALL to Array</button>
        </div>

        <div class="ptc-col" id="ptc-col-log">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <h3 style="margin:0; color:var(--primary-blue); font-size: 18px;"><i class="fa fa-terminal"></i> Live Log Output</h3>
                <div style="display:flex; align-items:center; gap:8px;">
                    <label style="color:#888; font-size:12px; cursor:pointer; display:flex; align-items:center; gap:4px;">
                        <input type="checkbox" id="auto_refresh" checked style="width:12px;height:12px;"> Auto Refresh
                    </label>
                    <button type="button" onclick="refreshLog();" style="padding: 4px 10px; font-size: 12px; cursor: pointer;">Refresh</button>
                </div>
            </div>
            <div id="ptc-log">Loading Logs...</div>
        </div>
    </div>
</form>

<script>
function refreshLog() {
    $.get('/plugins/plex_to_cache/plex_to_cache.php?action=log', function(data) {
        var logDiv = $('#ptc-log');
        logDiv.text(data);
        logDiv.scrollTop(logDiv[0].scrollHeight);
    });
}

function addMappingRow(dockerVal = '', hostVal = '') {
    var table = document.getElementById('mapping_table').getElementsByTagName('tbody')[0];
    var row = table.insertRow(-1);
    var cell1 = row.insertCell(0); var cell2 = row.insertCell(1); var cell3 = row.insertCell(2);
    cell1.innerHTML = '<input type="text" name="mapping_host[]" value="' + hostVal + '" class="ptc-input" style="padding:4px !important; height:26px !important;">';
    cell2.innerHTML = '<input type="text" name="mapping_docker[]" value="' + dockerVal + '" class="ptc-input" style="padding:4px !important; height:26px !important;">';
    cell3.innerHTML = '<a href="#" onclick="deleteRow(this); return false;" style="color:#ff4444; font-size:16px; margin-left:5px;"><i class="fa fa-minus-circle"></i></a>';
}

function deleteRow(btn) {
    var row = btn.parentNode.parentNode;
    row.parentNode.removeChild(row);
}

function testConnection(service, btn) {
    btn.textContent = '...';
    btn.className = 'btn-test';

    // Get current form values (not saved config)
    var url, key;
    if (service === 'plex') {
        url = $('input[name="PLEX_URL"]').val();
        key = $('input[name="PLEX_TOKEN"]').val();
    } else if (service === 'emby') {
        url = $('input[name="EMBY_URL"]').val();
        key = $('input[name="EMBY_API_KEY"]').val();
    } else if (service === 'jellyfin') {
        url = $('input[name="JELLYFIN_URL"]').val();
        key = $('input[name="JELLYFIN_API_KEY"]').val();
    }

    $.post('/plugins/plex_to_cache/plex_to_cache.php?action=test&service=' + service, {url: url, key: key}, function(data) {
        if (data.success) {
            btn.textContent = 'OK';
            btn.className = 'btn-test success';
        } else {
            btn.textContent = 'Fail';
            btn.className = 'btn-test error';
        }
        btn.title = data.message;
        setTimeout(function() {
            btn.textContent = 'Test';
            btn.className = 'btn-test';
        }, 3000);
    }, 'json').fail(function() {
        btn.textContent = 'Fail';
        btn.className = 'btn-test error';
        setTimeout(function() {
            btn.textContent = 'Test';
            btn.className = 'btn-test';
        }, 3000);
    });
}

function clearCache() {
    if (!confirm('This will move all media files cached by this plugin back to the array. Continue?')) return;
    alert('Moving cached media files... This may take a while.');
    $.getJSON('/plugins/plex_to_cache/plex_to_cache.php?action=clearcache', function(data) {
        alert(data.message);
        location.reload();
    }).fail(function() {
        alert('Failed to move cached media');
    });
}

function moveOther() {
    if (!confirm('This will move ALL files from cache to array EXCEPT the media files cached by this plugin. Continue?')) return;
    alert('Moving files... This may take a while. Check the log for progress.');
    $.getJSON('/plugins/plex_to_cache/plex_to_cache.php?action=moveother', function(data) {
        alert(data.message);
        refreshLog();
    }).fail(function() {
        alert('Failed to move files');
    });
}

function moveAll() {
    if (!confirm('This will move ALL files from cache to array, including media files cached by this plugin. Continue?')) return;
    alert('Moving all files... This may take a while. Check the log for progress.');
    $.getJSON('/plugins/plex_to_cache/plex_to_cache.php?action=moveall', function(data) {
        alert(data.message);
        location.reload();
    }).fail(function() {
        alert('Failed to move files');
    });
}

function serviceControl(cmd) {
    $.getJSON('/plugins/plex_to_cache/plex_to_cache.php?action=service&cmd=' + cmd, function(data) {
        var dot = document.getElementById('status-dot');
        if (data.running) {
            dot.className = 'status-dot running';
            dot.title = 'Running';
        } else {
            dot.className = 'status-dot stopped';
            dot.title = 'Stopped';
        }
    });
}

function updateCleanupUI() {
    var mode = document.querySelector('input[name="CLEANUP_MODE"]:checked').value;
    document.getElementById('smart-cleanup-options').style.display = mode === 'smart' ? 'block' : 'none';
    document.getElementById('days-cleanup-options').style.display = mode === 'days' ? 'block' : 'none';
    document.querySelectorAll('.radio-option').forEach(function(el) {
        el.classList.remove('selected');
    });
    document.querySelector('input[name="CLEANUP_MODE"]:checked').closest('.radio-option').classList.add('selected');
}

$(function() {
    <?php foreach ($mappings_pairs as $pair): ?>
    addMappingRow('<?= addslashes($pair[0]) ?>', '<?= addslashes($pair[1]) ?>');
    <?php endforeach; ?>
    if (document.getElementById('mapping_table').rows.length <= 1) { addMappingRow(); }
    refreshLog();
    setInterval(function() { if ($('#auto_refresh').is(':checked')) refreshLog(); }, 3000);
});
</script>
