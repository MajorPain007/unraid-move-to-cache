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
    "ENABLE_SMART_CLEANUP" => "False", "MOVIE_DELETE_DELAY" => "1800", "EPISODE_KEEP_PREVIOUS" => "2",
    "EXCLUDE_DIRS" => "", "MEDIA_FILETYPES" => ".mkv .mp4 .avi", "ARRAY_ROOT" => "/mnt/user",
    "CACHE_ROOT" => "/mnt/cache", "DOCKER_MAPPINGS" => ""
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

// AJAX: Test connection
if (isset($_GET['action']) && $_GET['action'] === 'test') {
    header('Content-Type: application/json');
    $service = $_GET['service'] ?? '';
    $result = ['success' => false, 'message' => 'Unknown service'];

    $ctx = stream_context_create(['ssl' => ['verify_peer' => false, 'verify_peer_name' => false], 'http' => ['timeout' => 5]]);

    if ($service === 'plex') {
        $url = rtrim($ptc_cfg['PLEX_URL'], '/') . '/identity';
        $opts = ['http' => ['header' => "X-Plex-Token: " . $ptc_cfg['PLEX_TOKEN'] . "\r\nAccept: application/json\r\n", 'timeout' => 5], 'ssl' => ['verify_peer' => false, 'verify_peer_name' => false]];
        $response = @file_get_contents($url, false, stream_context_create($opts));
        if ($response) {
            $data = json_decode($response, true);
            $name = $data['MediaContainer']['machineIdentifier'] ?? 'Unknown';
            $result = ['success' => true, 'message' => "Connected! Server ID: " . substr($name, 0, 8) . "..."];
        } else {
            $result = ['success' => false, 'message' => 'Connection failed. Check URL and Token.'];
        }
    } elseif ($service === 'emby' || $service === 'jellyfin') {
        $url_key = $service === 'emby' ? 'EMBY_URL' : 'JELLYFIN_URL';
        $api_key = $service === 'emby' ? 'EMBY_API_KEY' : 'JELLYFIN_API_KEY';
        $url = rtrim($ptc_cfg[$url_key], '/') . '/System/Info';
        $opts = ['http' => ['header' => "X-Emby-Token: " . $ptc_cfg[$api_key] . "\r\nAccept: application/json\r\n", 'timeout' => 5], 'ssl' => ['verify_peer' => false, 'verify_peer_name' => false]];
        $response = @file_get_contents($url, false, stream_context_create($opts));
        if ($response) {
            $data = json_decode($response, true);
            $name = $data['ServerName'] ?? 'Unknown';
            $result = ['success' => true, 'message' => "Connected! Server: $name"];
        } else {
            $result = ['success' => false, 'message' => 'Connection failed. Check URL and API Key.'];
        }
    }
    echo json_encode($result);
    exit;
}

// AJAX: Clear cache
if (isset($_GET['action']) && $_GET['action'] === 'clearcache') {
    header('Content-Type: application/json');
    $cache_root = $ptc_cfg['CACHE_ROOT'];
    $count = 0;
    $size = 0;

    if (is_dir($cache_root)) {
        $iterator = new RecursiveIteratorIterator(
            new RecursiveDirectoryIterator($cache_root, RecursiveDirectoryIterator::SKIP_DOTS),
            RecursiveIteratorIterator::CHILD_FIRST
        );

        $media_exts = array_map('trim', explode(' ', strtolower($ptc_cfg['MEDIA_FILETYPES'])));

        foreach ($iterator as $file) {
            if ($file->isFile()) {
                $ext = '.' . strtolower($file->getExtension());
                if (in_array($ext, $media_exts)) {
                    $size += $file->getSize();
                    @unlink($file->getPathname());
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

    $size_mb = round($size / 1024 / 1024, 2);
    echo json_encode(['success' => true, 'message' => "Cleared $count files ({$size_mb} MB)"]);
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
?>
<style>
:root { --primary-blue: #00aaff; --bg-dark: #111; --success-green: #00cc66; --error-red: #ff4444; }

#ptc-wrapper {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(450px, 1fr));
    gap: 20px;
    align-items: stretch;
    width: 100%;
    box-sizing: border-box;
    padding: 10px 0;
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

/* Status indicator */
.status-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: #1a1a1a;
    border-radius: 6px;
    padding: 10px 15px;
    margin-bottom: 15px;
}

.status-indicator {
    display: flex;
    align-items: center;
    gap: 8px;
    font-weight: bold;
}

.status-dot {
    width: 12px;
    height: 12px;
    border-radius: 50%;
    animation: pulse 2s infinite;
}

.status-dot.running { background: var(--success-green); }
.status-dot.stopped { background: var(--error-red); animation: none; }

@keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.5; }
}

.status-buttons { display: flex; gap: 8px; }
.status-buttons button {
    padding: 5px 12px;
    font-size: 12px;
    cursor: pointer;
    border: 1px solid #444;
    border-radius: 4px;
    background: #222;
    color: #fff;
}
.status-buttons button:hover { background: #333; border-color: var(--primary-blue); }

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

.btn-save-container { margin-bottom: 20px; }
.btn-save-container input[type="submit"] { width: 100%; padding: 12px; font-weight: bold; text-transform: uppercase; cursor: pointer; }

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

/* Clear cache button */
.btn-clear {
    padding: 8px 16px;
    font-size: 12px;
    cursor: pointer;
    border: 1px solid var(--error-red);
    border-radius: 4px;
    background: transparent;
    color: var(--error-red);
    margin-top: 15px;
}
.btn-clear:hover { background: var(--error-red); color: #fff; }
</style>

<form method="post" autocomplete="off">
    <div id="ptc-wrapper">
        <div class="ptc-col" id="ptc-col-servers">
            <div class="status-bar">
                <div class="status-indicator">
                    <span class="status-dot <?= $is_running ? 'running' : 'stopped' ?>" id="status-dot"></span>
                    <span id="status-text"><?= $is_running ? 'Running' : 'Stopped' ?></span>
                </div>
                <div class="status-buttons">
                    <button type="button" onclick="serviceControl('start')">Start</button>
                    <button type="button" onclick="serviceControl('stop')">Stop</button>
                    <button type="button" onclick="serviceControl('restart')">Restart</button>
                </div>
            </div>

            <div class="btn-save-container"><input type="submit" value="Save & Apply Settings"></div>

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

            <div class="section-header"><i class="fa fa-cogs"></i> Tuning & Cleanup</div>
            <div class="form-pair"><label data-tooltip="Interval in seconds to check for active streams.">Interval:</label><div class="form-input-wrapper"><input type="number" name="CHECK_INTERVAL" value="<?= htmlspecialchars($ptc_cfg['CHECK_INTERVAL']) ?>" class="ptc-input input-small"><span class="unit-label">sec</span></div></div>
            <div class="form-pair"><label data-tooltip="Delay before starting to copy files.">Copy Delay:</label><div class="form-input-wrapper"><input type="number" name="COPY_DELAY" value="<?= htmlspecialchars($ptc_cfg['COPY_DELAY']) ?>" class="ptc-input input-small"><span class="unit-label">sec</span></div></div>
            <div class="form-pair"><label data-tooltip="Maximum cache usage percentage before stopping copies.">Max Cache:</label><div class="form-input-wrapper"><input type="number" name="CACHE_MAX_USAGE" value="<?= htmlspecialchars($ptc_cfg['CACHE_MAX_USAGE']) ?>" class="ptc-input input-small"><span class="unit-label">%</span></div></div>
            <div class="form-pair"><label data-tooltip="Automatically remove watched media from cache.">Smart Clean:</label><div class="form-input-wrapper"><input type="checkbox" name="ENABLE_SMART_CLEANUP" value="True" <?= $ptc_cfg['ENABLE_SMART_CLEANUP'] == 'True' ? 'checked' : '' ?> ></div></div>
            <div class="form-pair"><label data-tooltip="Delay in seconds before deleting watched movies.">Delete Delay:</label><div class="form-input-wrapper"><input type="number" name="MOVIE_DELETE_DELAY" value="<?= htmlspecialchars($ptc_cfg['MOVIE_DELETE_DELAY']) ?>" class="ptc-input input-small"><span class="unit-label">sec</span></div></div>
            <div class="form-pair"><label data-tooltip="Number of previous episodes to keep in cache.">Keep Episodes:</label><div class="form-input-wrapper"><input type="number" name="EPISODE_KEEP_PREVIOUS" value="<?= htmlspecialchars($ptc_cfg['EPISODE_KEEP_PREVIOUS']) ?>" class="ptc-input input-small"><span class="unit-label">ep</span></div></div>

            <button type="button" class="btn-clear" onclick="clearCache()"><i class="fa fa-trash"></i> Clear All Cached Media</button>
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
    $.getJSON('/plugins/plex_to_cache/plex_to_cache.php?action=test&service=' + service, function(data) {
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
    }).fail(function() {
        btn.textContent = 'Fail';
        btn.className = 'btn-test error';
        setTimeout(function() {
            btn.textContent = 'Test';
            btn.className = 'btn-test';
        }, 3000);
    });
}

function clearCache() {
    if (!confirm('Are you sure you want to delete all cached media files? This cannot be undone.')) return;
    $.getJSON('/plugins/plex_to_cache/plex_to_cache.php?action=clearcache', function(data) {
        alert(data.message);
        refreshLog();
    }).fail(function() {
        alert('Failed to clear cache');
    });
}

function serviceControl(cmd) {
    $.getJSON('/plugins/plex_to_cache/plex_to_cache.php?action=service&cmd=' + cmd, function(data) {
        var dot = document.getElementById('status-dot');
        var text = document.getElementById('status-text');
        if (data.running) {
            dot.className = 'status-dot running';
            text.textContent = 'Running';
        } else {
            dot.className = 'status-dot stopped';
            text.textContent = 'Stopped';
        }
    });
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
