<?php
$ptc_plugin = "plex_to_cache";
$ptc_cfg_file = "/boot/config/plugins/$ptc_plugin/settings.cfg";
$ptc_log_file = "/var/log/plex_to_cache.log";
$ptc_pid_file = "/var/run/plex_to_cache.pid";

// Defaults
$ptc_cfg = [
    "ENABLE_PLEX" => "False", "PLEX_URL" => "http://localhost:32400", "PLEX_TOKEN" => "",
    "ENABLE_EMBY" => "False", "EMBY_URL" => "http://localhost:8096", "EMBY_API_KEY" => "",
    "ENABLE_JELLYFIN" => "False", "JELLYFIN_URL" => "http://localhost:8096", "JELLYFIN_API_KEY" => "",
    "CHECK_INTERVAL" => "10", "CACHE_MAX_USAGE" => "80", "COPY_DELAY" => "30",
    "CLEANUP_MODE" => "none", "MOVIE_DELETE_DELAY" => "1800", "EPISODE_KEEP_PREVIOUS" => "2",
    "CACHE_MAX_DAYS" => "7", "EXCLUDE_DIRS" => "", "MEDIA_FILETYPES" => ".mkv .mp4 .avi",
    "ARRAY_ROOT" => "/mnt/user", "CACHE_ROOT" => "/mnt/cache", "DOCKER_MAPPINGS" => "",
    "ENABLE_EPISODE_BATCHING" => "False", "EPISODE_BATCH_SIZE" => "30",
    "EPISODE_BATCH_TOLERANCE" => "10", "EPISODE_BATCH_PREFETCH" => "4",
    "ENABLE_CACHE_EVICTION" => "True", "ENABLE_NEXT_SEASON_PREFETCH" => "False",
    "ENABLE_FLUSH_SCHEDULE" => "False", "FLUSH_SCHEDULE_TIME" => "04:00",
    "FLUSH_SCOPE" => "tracked", "FLUSH_MIN_AGE" => "30"
];

$ptc_tracked_file  = "/boot/config/plugins/$ptc_plugin/cached_files.list";
$ptc_status_file   = "/var/run/plex_to_cache.status.json";
$ptc_request_file  = "/var/run/plex_to_cache.flush";
$ptc_daemon_script = "/usr/local/emhttp/plugins/$ptc_plugin/plex_to_cache.py";

/** The tracked list as path => timestamp. */
function ptc_tracked() {
    global $ptc_tracked_file;
    $out = [];
    if (!is_readable($ptc_tracked_file)) return $out;
    foreach (file($ptc_tracked_file, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) as $line) {
        $pos = strrpos($line, '|');
        if ($pos === false) { $out[$line] = 0; continue; }
        $out[substr($line, 0, $pos)] = (float)substr($line, $pos + 1);
    }
    return $out;
}

if (file_exists($ptc_cfg_file)) {
    $ptc_loaded = parse_ini_file($ptc_cfg_file);
    if ($ptc_loaded) { $ptc_cfg = array_merge($ptc_cfg, $ptc_loaded); }
}

/**
 * Unraid's CSRF token.
 *
 * Without it, any page an authenticated admin visits can trigger the
 * state-changing endpoints below - an <img src="...?action=service&cmd=stop">
 * is enough to stop the daemon. $var is normally provided by the page layout;
 * fall back to var.ini so the check also works when this file is requested
 * directly by the AJAX calls.
 */
function ptc_csrf_token() {
    global $var;
    if (isset($var['csrf_token']) && $var['csrf_token'] !== '') return $var['csrf_token'];
    $ini = @parse_ini_file('/usr/local/emhttp/state/var.ini');
    return ($ini && isset($ini['csrf_token'])) ? $ini['csrf_token'] : '';
}

function ptc_require_csrf($as_json = true) {
    $expected = ptc_csrf_token();
    if ($expected === '') return;                       // nothing to check against
    $got = isset($_REQUEST['csrf_token']) ? $_REQUEST['csrf_token'] : '';
    if (!hash_equals($expected, (string)$got)) {
        http_response_code(403);
        if ($as_json) {
            header('Content-Type: application/json');
            echo json_encode(['success' => false, 'message' => 'Invalid or missing CSRF token']);
        } else {
            header('Content-Type: text/plain');
            echo 'Invalid or missing CSRF token';
        }
        exit;
    }
}

// AJAX: Get log
if (isset($_GET['action']) && $_GET['action'] === 'log') {
    header('Content-Type: text/plain');
    echo file_exists($ptc_log_file) ? shell_exec("tail -n 200 " . escapeshellarg($ptc_log_file)) : "Log file not found. Service might be starting...";
    exit;
}

// AJAX: Daemon status snapshot (written periodically by the Python daemon)
if (isset($_GET['action']) && $_GET['action'] === 'status') {
    header('Content-Type: application/json');
    $ptc_status_file = '/var/run/plex_to_cache.status.json';
    echo file_exists($ptc_status_file) ? file_get_contents($ptc_status_file) : '{}';
    exit;
}

// AJAX: Test connection (uses form values sent via POST, not saved config)
if (isset($_GET['action']) && $_GET['action'] === 'test') {
    ptc_require_csrf();
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


// AJAX: Service control
if (isset($_GET['action']) && $_GET['action'] === 'service') {
    ptc_require_csrf();
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

// AJAX: What is on the cache right now, biggest first.
if (isset($_GET['action']) && $_GET['action'] === 'cached') {
    header('Content-Type: application/json');

    $streaming = [];
    if (is_readable($ptc_status_file)) {
        $st = json_decode(@file_get_contents($ptc_status_file), true);
        if (is_array($st) && !empty($st['active_streams'])) $streaming = $st['active_streams'];
    }

    $files = [];
    $total = 0;
    foreach (ptc_tracked() as $path => $ts) {
        $size = @filesize($path);
        if ($size === false) continue;   // gone since the list was written
        $total += $size;
        $files[] = [
            'path'   => $path,
            'name'   => basename($path),
            'dir'    => dirname($path),
            'size'   => $size,
            'age'    => $ts > 0 ? time() - (int)$ts : null,
            'in_use' => in_array(basename($path), $streaming, true),
        ];
    }
    usort($files, function ($a, $b) { return $b['size'] - $a['size']; });

    echo json_encode(['success' => true, 'files' => $files, 'total_bytes' => $total]);
    exit;
}

// AJAX: Move one file back to the array.
if (isset($_GET['action']) && $_GET['action'] === 'uncache') {
    ptc_require_csrf();
    header('Content-Type: application/json');

    $path = $_GET['path'] ?? '';
    // Only ever a file the plugin itself put on the cache. The daemon filters
    // the same way, so this is a better error message rather than the guard.
    if (!array_key_exists($path, ptc_tracked())) {
        echo json_encode(['success' => false, 'message' => 'Not a file this plugin cached']);
        exit;
    }

    $running = file_exists($ptc_pid_file) && posix_kill((int)@file_get_contents($ptc_pid_file), 0);
    if ($running) {
        if (@file_put_contents($ptc_request_file, "move\n" . $path . "\n") === false) {
            echo json_encode(['success' => false, 'message' => 'Cannot write the request file']);
            exit;
        }
        echo json_encode(['success' => true, 'message' => 'Queued: ' . basename($path)]);
    } else {
        if (!file_exists($ptc_daemon_script)) {
            echo json_encode(['success' => false, 'message' => 'plex_to_cache.py not found']);
            exit;
        }
        shell_exec('nohup python3 ' . escapeshellarg($ptc_daemon_script) . ' --flush '
                   . escapeshellarg($path) . ' >> /var/log/plex_to_cache.log 2>&1 &');
        echo json_encode(['success' => true, 'message' => 'Moving ' . basename($path)]);
    }
    exit;
}

// AJAX: Move everything this plugin cached back to the array.
// With the service running the request goes through a marker file, so the
// daemon does the work and keeps protecting files that are being streamed.
// With it stopped there is nobody to ask, so the script runs once directly.
if (isset($_GET['action']) && $_GET['action'] === 'flush') {
    ptc_require_csrf();
    header('Content-Type: application/json');

    $running = file_exists($ptc_pid_file) && posix_kill((int)@file_get_contents($ptc_pid_file), 0);
    if ($running) {
        if (@file_put_contents($ptc_request_file, "flush\n") === false) {
            echo json_encode(['success' => false, 'message' => 'Cannot write the request file']);
            exit;
        }
        echo json_encode(['success' => true,
                          'message' => 'Flush requested - the service picks it up within a few seconds.']);
    } else {
        if (!file_exists($ptc_daemon_script)) {
            echo json_encode(['success' => false, 'message' => 'plex_to_cache.py not found']);
            exit;
        }
        shell_exec('nohup python3 ' . escapeshellarg($ptc_daemon_script)
                   . ' --flush >> /var/log/plex_to_cache.log 2>&1 &');
        echo json_encode(['success' => true,
                          'message' => 'Service is stopped - running the flush directly. Watch the log.']);
    }
    exit;
}

// POST: Save settings
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    ptc_require_csrf(false);
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

    // condrestart: apply new settings if the service is enabled, but do not
    // force-start a service the user has stopped on purpose.
    shell_exec("/usr/local/emhttp/plugins/plex_to_cache/scripts/rc.plex_to_cache condrestart > /dev/null 2>&1 &");
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
<input type="hidden" name="csrf_token" value="<?= htmlspecialchars(ptc_csrf_token(), ENT_QUOTES) ?>">
    <div id="ptc-wrapper">
        <div class="ptc-col" id="ptc-col-servers">
            <div class="top-control-bar">
                <input type="submit" value="Save & Apply">
                <button type="button" class="service-btn" onclick="serviceControl('start')">Start</button>
                <button type="button" class="service-btn" onclick="serviceControl('stop')">Stop</button>
                <button type="button" class="service-btn" id="flush-btn" onclick="flushCache(this)"
                        title="Move every file this plugin cached back to the array. Files being streamed right now are left alone.">Empty Cache</button>
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
            <div class="form-pair"><label data-tooltip="When the cache is full, move the oldest plugin-cached files back to the array to make room for the currently streamed media (LRU). Active streams and queued files are never evicted.">Eviction:</label><div class="form-input-wrapper"><input type="checkbox" name="ENABLE_CACHE_EVICTION" value="True" <?= $ptc_cfg['ENABLE_CACHE_EVICTION'] == 'True' ? 'checked' : '' ?>></div></div>

            <div class="section-header"><i class="fa fa-download"></i> Empty Cache</div>
            <div class="form-pair"><label data-tooltip="What the Empty Cache button and the schedule move back. 'Cached by plugin' touches only files this plugin copied to the cache. 'All media in mapped folders' also moves files it never touched. Only the folders from your docker mappings are walked - a separate downloads share on the same pool is not touched.">Scope:</label><div class="form-input-wrapper"><select name="FLUSH_SCOPE" class="ptc-input" onchange="updateFlushUI()">
                <option value="tracked" <?= $ptc_cfg['FLUSH_SCOPE'] != 'all' ? 'selected' : '' ?>>Cached by plugin</option>
                <option value="all" <?= $ptc_cfg['FLUSH_SCOPE'] == 'all' ? 'selected' : '' ?>>All media in mapped folders</option>
            </select></div></div>
            <div id="flush-scope-note" class="cleanup-options" style="display: <?= $ptc_cfg['FLUSH_SCOPE'] == 'all' ? 'block' : 'none' ?>;">
                <div class="radio-desc" style="margin-bottom:8px;">Also moves files this plugin never copied. A file whose name already exists on the array is skipped rather than overwritten, and anything modified recently is left alone so a file still being written is not moved out from under the process writing it.</div>
                <div class="form-pair"><label data-tooltip="Leave files alone that were modified within this many minutes. Guards against moving a file that is still being written.">Min. Age:</label><div class="form-input-wrapper"><input type="number" min="0" name="FLUSH_MIN_AGE" value="<?= htmlspecialchars($ptc_cfg['FLUSH_MIN_AGE']) ?>" class="ptc-input input-small"><span class="unit-label">min</span></div></div>
            </div>
            <div class="form-pair"><label data-tooltip="Empty the cache once a day at a fixed time. If the server was off or the service stopped at that time, it runs at the next opportunity instead of skipping the day.">Daily:</label><div class="form-input-wrapper"><input type="checkbox" name="ENABLE_FLUSH_SCHEDULE" value="True" <?= $ptc_cfg['ENABLE_FLUSH_SCHEDULE'] == 'True' ? 'checked' : '' ?> onchange="updateFlushUI()"></div></div>
            <div id="flush-schedule-options" class="cleanup-options" style="display: <?= $ptc_cfg['ENABLE_FLUSH_SCHEDULE'] == 'True' ? 'block' : 'none' ?>;">
                <div class="form-pair"><label data-tooltip="Time of day, 24-hour HH:MM.">At:</label><div class="form-input-wrapper"><input type="text" name="FLUSH_SCHEDULE_TIME" value="<?= htmlspecialchars($ptc_cfg['FLUSH_SCHEDULE_TIME']) ?>" placeholder="04:00" pattern="[0-2][0-9]:[0-5][0-9]" class="ptc-input input-small"></div></div>
            </div>

            <div class="section-header"><i class="fa fa-list-ol"></i> Season Batching</div>
            <div class="form-pair"><label data-tooltip="For long seasons, only cache one batch of episodes at a time instead of the whole season. The next batch starts copying automatically shortly before the current one runs out.">Enable:</label><div class="form-input-wrapper"><input type="checkbox" name="ENABLE_EPISODE_BATCHING" value="True" <?= $ptc_cfg['ENABLE_EPISODE_BATCHING'] == 'True' ? 'checked' : '' ?> onchange="updateBatchingUI()"></div></div>
            <div id="batching-options" class="cleanup-options" style="display: <?= $ptc_cfg['ENABLE_EPISODE_BATCHING'] == 'True' ? 'block' : 'none' ?>;">
                <div class="form-pair"><label data-tooltip="Number of episodes cached per batch.">Batch Size:</label><div class="form-input-wrapper"><input type="number" min="1" name="EPISODE_BATCH_SIZE" value="<?= htmlspecialchars($ptc_cfg['EPISODE_BATCH_SIZE']) ?>" class="ptc-input input-small"><span class="unit-label">ep</span></div></div>
                <div class="form-pair"><label data-tooltip="Buffer: if a season exceeds the batch size by no more than this many episodes, it is cached completely instead of split (e.g. 36 episodes with batch 30 + buffer 10 = all at once).">Buffer:</label><div class="form-input-wrapper"><input type="number" min="0" name="EPISODE_BATCH_TOLERANCE" value="<?= htmlspecialchars($ptc_cfg['EPISODE_BATCH_TOLERANCE']) ?>" class="ptc-input input-small"><span class="unit-label">ep</span></div></div>
                <div class="form-pair"><label data-tooltip="Start caching the next batch when this many episodes remain in the current batch (e.g. 4 = next batch starts at episode 26 of 30).">Prefetch At:</label><div class="form-input-wrapper"><input type="number" min="0" name="EPISODE_BATCH_PREFETCH" value="<?= htmlspecialchars($ptc_cfg['EPISODE_BATCH_PREFETCH']) ?>" class="ptc-input input-small"><span class="unit-label">ep left</span></div></div>
            </div>
            <div class="form-pair"><label data-tooltip="Near the end of a season, pre-cache the beginning of the next season (first batch if batching is enabled, otherwise the whole season). Uses the 'Prefetch At' threshold.">Next Season:</label><div class="form-input-wrapper"><input type="checkbox" name="ENABLE_NEXT_SEASON_PREFETCH" value="True" <?= $ptc_cfg['ENABLE_NEXT_SEASON_PREFETCH'] == 'True' ? 'checked' : '' ?>></div></div>

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

        </div>

        <div class="ptc-col" id="ptc-col-log">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <h3 style="margin:0; color:var(--primary-blue); font-size: 18px;"><i class="fa fa-hdd-o"></i> On Cache</h3>
                <button type="button" onclick="refreshCached();" style="padding: 4px 10px; font-size: 12px; cursor: pointer;">Refresh</button>
            </div>
            <div id="ptc-cached-summary" style="color:#888; font-size:12px; margin-top:6px;"></div>
            <div id="ptc-cached-wrap" style="max-height:240px; overflow:auto; margin:6px 0 18px;">
                <table id="ptc-cached-table" style="width:100%; font-size:12px; border-collapse:collapse;">
                    <thead><tr>
                        <th style="text-align:left; padding:4px 6px;">File</th>
                        <th style="text-align:right; padding:4px 6px; white-space:nowrap;">Size</th>
                        <th style="text-align:right; padding:4px 6px; white-space:nowrap;">Cached</th>
                        <th style="padding:4px 6px;"></th>
                    </tr></thead>
                    <tbody><tr><td colspan="4" style="padding:6px; color:#888;">Loading...</td></tr></tbody>
                </table>
            </div>

            <div style="display:flex; justify-content:space-between; align-items:center;">
                <h3 style="margin:0; color:var(--primary-blue); font-size: 18px;"><i class="fa fa-terminal"></i> Live Log Output</h3>
                <div style="display:flex; align-items:center; gap:8px;">
                    <label style="color:#888; font-size:12px; cursor:pointer; display:flex; align-items:center; gap:4px;">
                        <input type="checkbox" id="auto_refresh" checked style="width:12px;height:12px;"> Auto Refresh
                    </label>
                    <button type="button" onclick="refreshLog();" style="padding: 4px 10px; font-size: 12px; cursor: pointer;">Refresh</button>
                </div>
            </div>
            <div id="ptc-status" style="color:#888; font-size:12px; margin-top:8px; min-height:16px;"></div>
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

var ptcLastFlushSeen = 0;

function refreshStatus() {
    $.getJSON('/plugins/plex_to_cache/plex_to_cache.php?action=status', function(d) {
        if (!d || !d.updated) { $('#ptc-status').text(''); return; }
        var parts = [];
        parts.push('Cached: ' + d.cached_files + ' files / ' + (d.cached_bytes / 1073741824).toFixed(1) + ' GB');
        if (d.cache_usage_pct !== null && d.cache_usage_pct !== undefined) parts.push('Cache used: ' + d.cache_usage_pct + '%');
        parts.push('Queue: ' + d.queue_length);
        if (d.copying) parts.push('Copying: ' + d.copying);
        if (d.active_streams && d.active_streams.length) parts.push('Streams: ' + d.active_streams.length);

        var f = d.flush;
        if (f && f.active) {
            parts.push('Emptying cache: ' + f.done + '/' + f.total
                       + ' (' + (f.bytes / 1073741824).toFixed(1) + ' GB)'
                       + (f.skipped ? ', ' + f.skipped + ' in use' : '')
                       + (f.conflicts ? ', ' + f.conflicts + ' name clashes' : ''));
        }
        // Refresh the file list once when a flush finishes, so it does not sit
        // there showing files that are no longer on the cache.
        if (f && f.finished && f.finished !== ptcLastFlushSeen) {
            ptcLastFlushSeen = f.finished;
            refreshCached();
        }
        // Keep the button disabled across the gap between requesting a flush
        // and the service picking the request up, or the next poll would
        // re-enable it and invite a second click.
        $('#flush-btn').prop('disabled',
            !!(f && f.active) || (Date.now() - ptcFlushRequested < 15000));

        $('#ptc-status').text(parts.join('  ·  '));
    }).fail(function() { $('#ptc-status').text(''); });
}

function ptcBytes(n) {
    if (n >= 1073741824) return (n / 1073741824).toFixed(1) + ' GB';
    if (n >= 1048576)    return (n / 1048576).toFixed(0) + ' MB';
    return (n / 1024).toFixed(0) + ' KB';
}

function ptcAge(sec) {
    if (sec === null || sec === undefined) return '';
    if (sec < 3600)  return Math.round(sec / 60) + ' min ago';
    if (sec < 86400) return Math.round(sec / 3600) + ' h ago';
    return Math.round(sec / 86400) + ' d ago';
}

function refreshCached() {
    $.getJSON('/plugins/plex_to_cache/plex_to_cache.php?action=cached', function(d) {
        var body = $('#ptc-cached-table tbody').empty();
        if (!d || !d.success || !d.files.length) {
            $('#ptc-cached-summary').text('Nothing cached by the plugin right now.');
            body.append('<tr><td colspan="4" style="padding:6px; color:#888;">Empty</td></tr>');
            return;
        }
        $('#ptc-cached-summary').text(d.files.length + ' files  ·  ' + ptcBytes(d.total_bytes));
        d.files.forEach(function(f) {
            var tr = $('<tr>').css('border-top', '1px solid #222');
            $('<td>').css({padding: '4px 6px', wordBreak: 'break-all'})
                     .attr('title', f.dir)
                     .text(f.name + (f.in_use ? '  (streaming)' : '')).appendTo(tr);
            $('<td>').css({padding: '4px 6px', textAlign: 'right', whiteSpace: 'nowrap'})
                     .text(ptcBytes(f.size)).appendTo(tr);
            $('<td>').css({padding: '4px 6px', textAlign: 'right', whiteSpace: 'nowrap', color: '#888'})
                     .text(ptcAge(f.age)).appendTo(tr);
            var cell = $('<td>').css({padding: '4px 6px', textAlign: 'right'}).appendTo(tr);
            $('<button type="button" class="btn-test">')
                .text(f.in_use ? 'in use' : 'To Array')
                .prop('disabled', f.in_use)
                .attr('title', f.in_use
                      ? 'This file is being streamed - it stays on the cache'
                      : 'Move this file back to the array')
                .on('click', function() { uncacheFile(f.path, this); })
                .appendTo(cell);
            body.append(tr);
        });
    }).fail(function() {
        $('#ptc-cached-summary').text('Could not read the cached-files list.');
    });
}

function uncacheFile(path, btn) {
    btn.disabled = true;
    btn.textContent = '...';
    $.getJSON('/plugins/plex_to_cache/plex_to_cache.php?action=uncache'
              + '&path=' + encodeURIComponent(path)
              + '&csrf_token=' + encodeURIComponent(ptcToken), function(data) {
        $('#ptc-status').text(data.message || '');
        if (!data.success) { btn.disabled = false; btn.textContent = 'To Array'; }
        setTimeout(refreshCached, 4000);
    }).fail(function() {
        $('#ptc-status').text('Request failed.');
        btn.disabled = false;
        btn.textContent = 'To Array';
    });
}

function updateFlushUI() {
    var scope = $('select[name="FLUSH_SCOPE"]').val();
    $('#flush-scope-note').toggle(scope === 'all');
    $('#flush-schedule-options').toggle($('input[name="ENABLE_FLUSH_SCHEDULE"]').is(':checked'));
}

var ptcFlushRequested = 0;

function flushCache(btn) {
    if (!confirm('Move every file this plugin cached back to the array?\n\n'
                 + 'Files being streamed right now are left on the cache. '
                 + 'Nothing else on the cache pool is touched.')) return;
    btn.disabled = true;
    ptcFlushRequested = Date.now();
    $.getJSON('/plugins/plex_to_cache/plex_to_cache.php?action=flush'
              + '&csrf_token=' + encodeURIComponent(ptcToken), function(data) {
        $('#ptc-status').text(data.message || '');
        if (!data.success) { ptcFlushRequested = 0; btn.disabled = false; }
        setTimeout(refreshLog, 2000);
    }).fail(function() {
        $('#ptc-status').text('Flush request failed.');
        ptcFlushRequested = 0;
        btn.disabled = false;
    });
}

// Unraid exposes csrf_token as a global on its own pages; fall back to the
// hidden field in the form so the AJAX calls work either way.
var ptcToken = (typeof csrf_token !== 'undefined' && csrf_token)
    ? csrf_token
    : ($('input[name="csrf_token"]').val() || '');

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

    $.post('/plugins/plex_to_cache/plex_to_cache.php?action=test&service=' + service,
            {url: url, key: key, csrf_token: ptcToken}, function(data) {
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


function serviceControl(cmd) {
    $.getJSON('/plugins/plex_to_cache/plex_to_cache.php?action=service&cmd=' + cmd
              + '&csrf_token=' + encodeURIComponent(ptcToken), function(data) {
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

function updateBatchingUI() {
    var enabled = document.querySelector('input[name="ENABLE_EPISODE_BATCHING"]').checked;
    document.getElementById('batching-options').style.display = enabled ? 'block' : 'none';
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
    refreshStatus();
    refreshCached();
    updateFlushUI();
    setInterval(function() {
        if ($('#auto_refresh').is(':checked')) { refreshLog(); refreshStatus(); }
    }, 3000);
    // The file list changes far less often than the log, and reading it walks
    // the tracked list on flash - once a minute is plenty.
    setInterval(function() {
        if ($('#auto_refresh').is(':checked')) { refreshCached(); }
    }, 60000);
});
</script>
