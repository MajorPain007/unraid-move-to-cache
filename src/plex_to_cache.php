<?php
$ptc_plugin = "plex_to_cache";
$ptc_cfg_file = "/boot/config/plugins/$ptc_plugin/settings.cfg";
$ptc_log_file = "/var/log/plex_to_cache.log";

// Defaults inkl. deiner Docker Mappings
$ptc_cfg = [
    "ENABLE_PLEX" => "False",
    "PLEX_URL" => "http://localhost:32400",
    "PLEX_TOKEN" => "",
    "ENABLE_EMBY" => "False",
    "EMBY_URL" => "http://localhost:8096",
    "EMBY_API_KEY" => "",
    "ENABLE_JELLYFIN" => "False",
    "JELLYFIN_URL" => "http://localhost:8096",
    "JELLYFIN_API_KEY" => "",
    "CHECK_INTERVAL" => "10",
    "CACHE_MAX_USAGE" => "80",
    "COPY_DELAY" => "30",
    "ENABLE_SMART_CLEANUP" => "False",
    "MOVIE_DELETE_DELAY" => "1800",
    "EPISODE_KEEP_PREVIOUS" => "2",
    "EXCLUDE_DIRS" => "",
    "MEDIA_FILETYPES" => ".mkv .mp4 .avi",
    "ARRAY_ROOT" => "/mnt/user",
    "CACHE_ROOT" => "/mnt/cache",
    "DOCKER_MAPPINGS" => "/media:/Media;/movies:/Media/Movies;/tv:/Media/TV"
];

// Load Config
if (file_exists($ptc_cfg_file)) {
    $ptc_loaded = parse_ini_file($ptc_cfg_file);
    if ($ptc_loaded) {
        $ptc_cfg = array_merge($ptc_cfg, $ptc_loaded);
    }
}

// Handle Submit
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    foreach ($ptc_cfg as $key => $val) {
        if (isset($_POST[$key])) {
            $ptc_cfg[$key] = $_POST[$key];
        } else {
            if (strpos($key, "ENABLE_") === 0 || $key === "ENABLE_SMART_CLEANUP") {
                 $ptc_cfg[$key] = "False";
            }
        }
    }
    
    // Process Docker Mappings Table
    $mappings_str = "";
    if (isset($_POST['mapping_docker']) && isset($_POST['mapping_host'])) {
        $dockers = $_POST['mapping_docker'];
        $hosts = $_POST['mapping_host'];
        $pairs = [];
        for ($i = 0; $i < count($dockers); $i++) {
            $d = trim($dockers[$i]);
            $h = trim($hosts[$i]);
            if (!empty($d) && !empty($h)) {
                $pairs[] = "$d:$h";
            }
        }
        $mappings_str = implode(";", $pairs);
    }
    $ptc_cfg['DOCKER_MAPPINGS'] = $mappings_str;

    // Write Config
    $content = "";
    foreach ($ptc_cfg as $key => $val) {
        $content .= "$key=\"$val\"\n";
    }
    
    if (!is_dir(dirname($ptc_cfg_file))) mkdir(dirname($ptc_cfg_file), 0777, true);
    file_put_contents($ptc_cfg_file, $content);
    
    shell_exec("/usr/local/emhttp/plugins/plex_to_cache/scripts/rc.plex_to_cache restart");
    $save_msg = "Settings Saved & Service Restarted";
}

// Prepare Mappings for JS
$mappings_pairs = [];
if (!empty($ptc_cfg['DOCKER_MAPPINGS'])) {
    $pairs = explode(";", $ptc_cfg['DOCKER_MAPPINGS']);
    foreach ($pairs as $pair) {
        if (strpos($pair, ":") !== false) {
            $mappings_pairs[] = explode(":", $pair, 2);
        }
    }
}
?>

<style>
:root {
    --primary-blue: #00aaff;
    --bg-dark: #111;
}

#ptc-wrapper {
    display: flex;
    flex-wrap: nowrap;
    align-items: flex-start;
    justify-content: space-between;
    gap: 20px;
    width: 100%;
    box-sizing: border-box;
    padding: 10px 0;
}

#ptc-settings, #ptc-log-container {
    background: var(--bg-dark);
    border-radius: 12px;
    box-shadow: 0 0 12px rgba(0, 170, 255, 0.2);
    color: #f0f8ff;
    padding: 20px;
    box-sizing: border-box;
}

#ptc-settings {
    flex: 0 0 48%;
}

#ptc-log-container {
    flex: 0 0 50%;
    display: flex;
    flex-direction: column;
    min-height: 850px;
}

.section-header {
    color: var(--primary-blue);
    font-size: 18px;
    font-weight: bold;
    margin-bottom: 15px;
    margin-top: 25px;
    border-bottom: 1px solid #333;
    padding-bottom: 5px;
}
.section-header:first-of-type { margin-top: 0; }

.form-pair {
    display: flex;
    align-items: center;
    margin-bottom: 15px;
    gap: 10px;
}

.form-pair label {
    flex: 0 0 140px;
    color: var(--primary-blue);
    font-weight: bold;
    font-size: 14px;
}

.form-input-wrapper {
    flex: 1;
    display: flex;
    align-items: center;
}

.form-input-wrapper input[type="text"],
.form-input-wrapper input[type="password"],
.form-input-wrapper input[type="number"] {
    background: #111;
    border: 1px solid var(--primary-blue);
    border-radius: 5px;
    color: #fff;
    padding: 8px;
    width: 100%;
    box-sizing: border-box;
}

.form-input-wrapper input[type="checkbox"] {
    accent-color: var(--primary-blue);
    width: 18px;
    height: 18px;
    cursor: pointer;
}

.help-text {
    font-size: 12px;
    color: #888;
    margin-left: 8px;
    font-style: italic;
}

#mapping_table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 10px;
}
#mapping_table th {
    text-align: left;
    color: var(--primary-blue);
    padding: 8px;
    border-bottom: 1px solid #333;
    font-size: 13px;
}
#mapping_table td {
    padding: 5px;
}

.btn-save {
    background: var(--primary-blue);
    color: #fff;
    border: none;
    border-radius: 4px;
    padding: 10px 20px;
    cursor: pointer;
    font-weight: bold;
    font-size: 14px;
}
.btn-save:hover { filter: brightness(1.1); }

.btn-add {
    background: transparent;
    color: var(--primary-blue);
    border: 1px solid var(--primary-blue);
    padding: 5px 10px;
    border-radius: 4px;
    cursor: pointer;
    margin-top: 10px;
}
.btn-add:hover { background: rgba(0, 170, 255, 0.1); }

#ptc-log {
    background: #000;
    border: 1px solid var(--primary-blue);
    border-radius: 8px;
    color: #00ffaa;
    font-family: monospace;
    font-size: 12px;
    padding: 15px;
    flex-grow: 1;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-all;
}

.status-msg {
    background: rgba(46, 204, 64, 0.1);
    color: #2ECC40;
    padding: 10px;
    border-radius: 6px;
    margin-bottom: 20px;
    border: 1px solid #2ECC40;
    text-align: center;
    font-weight: bold;
}

@media (max-width: 1100px) {
    #ptc-wrapper { flex-wrap: wrap; }
    #ptc-settings, #ptc-log-container { flex: 1 1 100%; }
}
</style>

<div id="ptc-wrapper">

    <!-- LEFT COLUMN: SETTINGS -->
    <div id="ptc-settings">
        <!-- autocomplete="off" to prevent password manager autofill -->
        <form method="post" autocomplete="off">
            <?php if (isset($save_msg)) echo "<div class='status-msg'>$save_msg</div>"; ?>
            
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
                <h2 style="margin:0; color:var(--primary-blue);">Settings</h2>
                <input type="submit" value="Save & Apply" class="btn-save">
            </div>

            <!-- PLEX -->
            <div class="section-header"><i class="fa fa-play-circle"></i> Plex Media Server</div>
            <div class="form-pair">
                <label>Enable Plex:</label>
                <div class="form-input-wrapper">
                    <input type="checkbox" name="ENABLE_PLEX" value="True" <?= $ptc_cfg['ENABLE_PLEX'] == 'True' ? 'checked' : '' ?> >
                </div>
            </div>
            <div class="form-pair">
                <label>Plex URL:</label>
                <div class="form-input-wrapper">
                    <input type="text" name="PLEX_URL" value="<?= $ptc_cfg['PLEX_URL'] ?>" placeholder="http://192.168.1.100:32400" autocomplete="off">
                </div>
            </div>
            <div class="form-pair">
                <label>Plex Token:</label>
                <div class="form-input-wrapper">
                    <input type="password" name="PLEX_TOKEN" value="<?= $ptc_cfg['PLEX_TOKEN'] ?>" onmouseover="this.type='text'" onmouseout="this.type='password'" autocomplete="new-password">
                </div>
            </div>

            <!-- EMBY -->
            <div class="section-header"><i class="fa fa-server"></i> Emby Server</div>
            <div class="form-pair">
                <label>Enable Emby:</label>
                <div class="form-input-wrapper">
                    <input type="checkbox" name="ENABLE_EMBY" value="True" <?= $ptc_cfg['ENABLE_EMBY'] == 'True' ? 'checked' : '' ?> >
                </div>
            </div>
            <div class="form-pair">
                <label>Emby URL:</label>
                <div class="form-input-wrapper">
                    <input type="text" name="EMBY_URL" value="<?= $ptc_cfg['EMBY_URL'] ?>" placeholder="http://192.168.1.100:8096" autocomplete="off">
                </div>
            </div>
            <div class="form-pair">
                <label>API Key:</label>
                <div class="form-input-wrapper">
                    <input type="password" name="EMBY_API_KEY" value="<?= $ptc_cfg['EMBY_API_KEY'] ?>" onmouseover="this.type='text'" onmouseout="this.type='password'" autocomplete="new-password">
                </div>
            </div>

            <!-- JELLYFIN -->
            <div class="section-header"><i class="fa fa-film"></i> Jellyfin Server</div>
            <div class="form-pair">
                <label>Enable Jellyfin:</label>
                <div class="form-input-wrapper">
                    <input type="checkbox" name="ENABLE_JELLYFIN" value="True" <?= $ptc_cfg['ENABLE_JELLYFIN'] == 'True' ? 'checked' : '' ?> >
                </div>
            </div>
            <div class="form-pair">
                <label>Jellyfin URL:</label>
                <div class="form-input-wrapper">
                    <input type="text" name="JELLYFIN_URL" value="<?= $ptc_cfg['JELLYFIN_URL'] ?>" placeholder="http://192.168.1.100:8096" autocomplete="off">
                </div>
            </div>
            <div class="form-pair">
                <label>API Key:</label>
                <div class="form-input-wrapper">
                    <input type="password" name="JELLYFIN_API_KEY" value="<?= $ptc_cfg['JELLYFIN_API_KEY'] ?>" onmouseover="this.type='text'" onmouseout="this.type='password'" autocomplete="new-password">
                </div>
            </div>

            <!-- STORAGE -->
            <div class="section-header"><i class="fa fa-folder-open"></i> Storage & Paths</div>
            <div class="form-pair">
                <label>Array Root:</label>
                <div class="form-input-wrapper">
                    <input type="text" name="ARRAY_ROOT" value="<?= $ptc_cfg['ARRAY_ROOT'] ?>" autocomplete="off">
                </div>
            </div>
            <div class="form-pair">
                <label>Cache Root:</label>
                <div class="form-input-wrapper">
                    <input type="text" name="CACHE_ROOT" value="<?= $ptc_cfg['CACHE_ROOT'] ?>" autocomplete="off">
                </div>
            </div>
            <div class="form-pair">
                <label>Exclude Dirs:</label>
                <div class="form-input-wrapper">
                    <input type="text" name="EXCLUDE_DIRS" value="<?= $ptc_cfg['EXCLUDE_DIRS'] ?>" placeholder="temp,downloads" autocomplete="off">
                </div>
            </div>

            <!-- MAPPINGS -->
            <div class="section-header"><i class="fa fa-exchange"></i> Docker Mappings</div>
            <table id="mapping_table">
                <thead>
                    <tr>
                        <th style="width: 45%;">Docker Path (Container)</th>
                        <th style="width: 45%;">Host Path (Unraid)</th>
                        <th style="width: 10%;"></th>
                    </tr>
                </thead>
                <tbody></tbody>
            </table>
            <button type="button" class="btn-add" onclick="addMappingRow()">+ Add Mapping</button>

            <!-- TUNING -->
            <div class="section-header"><i class="fa fa-cogs"></i> Tuning & Cleanup</div>
            <div class="form-pair">
                <label>Check Interval:</label>
                <div class="form-input-wrapper">
                    <input type="number" name="CHECK_INTERVAL" value="<?= $ptc_cfg['CHECK_INTERVAL'] ?>" style="width: 100px;">
                    <span class="help-text">seconds</span>
                </div>
            </div>
            <div class="form-pair">
                <label>Copy Delay:</label>
                <div class="form-input-wrapper">
                    <input type="number" name="COPY_DELAY" value="<?= $ptc_cfg['COPY_DELAY'] ?>" style="width: 100px;">
                    <span class="help-text">seconds</span>
                </div>
            </div>
            <div class="form-pair">
                <label>Max Cache Usage:</label>
                <div class="form-input-wrapper">
                    <input type="number" name="CACHE_MAX_USAGE" value="<?= $ptc_cfg['CACHE_MAX_USAGE'] ?>" style="width: 100px;">
                    <span class="help-text">%</span>
                </div>
            </div>
            <div class="form-pair">
                <label>Smart Cleanup:</label>
                <div class="form-input-wrapper">
                    <input type="checkbox" name="ENABLE_SMART_CLEANUP" value="True" <?= $ptc_cfg['ENABLE_SMART_CLEANUP'] == 'True' ? 'checked' : '' ?> >
                </div>
            </div>
            <div class="form-pair">
                <label>Delete Delay:</label>
                <div class="form-input-wrapper">
                    <input type="number" name="MOVIE_DELETE_DELAY" value="<?= $ptc_cfg['MOVIE_DELETE_DELAY'] ?>" style="width: 100px;">
                    <span class="help-text">seconds after watching</span>
                </div>
            </div>
            <div class="form-pair">
                <label>Keep Episodes:</label>
                <div class="form-input-wrapper">
                    <input type="number" name="EPISODE_KEEP_PREVIOUS" value="<?= $ptc_cfg['EPISODE_KEEP_PREVIOUS'] ?>" style="width: 100px;">
                    <span class="help-text">count</span>
                </div>
            </div>

        </form>
    </div>

    <!-- RIGHT COLUMN: LOGS -->
    <div id="ptc-log-container">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 15px;">
            <h3 style="margin:0; color:var(--primary-blue);"><i class="fa fa-terminal"></i> Log Output</h3>
            <button class="btn-add" onclick="location.reload();" style="margin:0;">Refresh</button>
        </div>
        <div id="ptc-log">
            <?php
            if (file_exists($ptc_log_file)) {
                $output = shell_exec("tail -n 100 " . escapeshellarg($ptc_log_file));
                if (empty($output)) {
                    echo "Log file exists but is empty. Service starting...";
                } else {
                    echo nl2br(htmlspecialchars((string)$output));
                }
            } else {
                echo "Log file not found at $ptc_log_file. Service might be stopped or starting...";
            }
            ?>
        </div>
    </div>

</div>

<script>
function addMappingRow(dockerVal = '', hostVal = '') {
    var table = document.getElementById('mapping_table').getElementsByTagName('tbody')[0];
    var row = table.insertRow(-1);
    var cell1 = row.insertCell(0);
    var cell2 = row.insertCell(1);
    var cell3 = row.insertCell(2);
    
    cell1.innerHTML = '<input type="text" name="mapping_docker[]" value="' + dockerVal + '" placeholder="/movies" style="width:100%; background:#111; border:1px solid var(--primary-blue); color:#fff; padding:6px; border-radius:4px;" autocomplete="off">';
    cell2.innerHTML = '<input type="text" name="mapping_host[]" value="' + hostVal + '" placeholder="/mnt/user/Media/Movies" style="width:100%; background:#111; border:1px solid var(--primary-blue); color:#fff; padding:6px; border-radius:4px;" autocomplete="off">';
    cell3.innerHTML = '<a href="#" onclick="deleteRow(this); return false;" style="color:#ff4444; font-size:18px;"><i class="fa fa-minus-circle"></i></a>';
    cell3.style.textAlign = "center";
}

function deleteRow(btn) {
    var row = btn.parentNode.parentNode;
    row.parentNode.removeChild(row);
}

// Init rows
$(function() {
    <?php foreach ($mappings_pairs as $pair): ?>
    addMappingRow('<?= addslashes($pair[0]) ?>', '<?= addslashes($pair[1]) ?>');
    <?php endforeach; ?>
    // Don't add an empty row if we already have some
    if (document.getElementById('mapping_table').rows.length <= 1) {
        addMappingRow();
    }
});
</script>
