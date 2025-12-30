<?php
$ptc_plugin = "plex_to_cache";
$ptc_cfg_file = "/boot/config/plugins/$ptc_plugin/settings.cfg";
$ptc_log_file = "/var/log/plex_to_cache.log";

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
?>
<style>
:root { --primary-blue: #00aaff; --bg-dark: #111; }
#ptc-wrapper { display: flex; flex-wrap: nowrap; align-items: stretch; justify-content: space-between; gap: 8px; width: 100%; box-sizing: border-box; padding: 10px 0; }
.ptc-col { background: var(--bg-dark); border-radius: 8px; box-shadow: 0 0 10px rgba(0, 170, 255, 0.15); color: #f0f8ff; padding: 18px; box-sizing: border-box; display: flex; flex-direction: column; flex: 1; }
#ptc-col-servers { flex: 0 0 28%; }
#ptc-col-tuning { flex: 0 0 32%; }
#ptc-col-log { flex: 0 0 39%; }

.section-header { color: var(--primary-blue); font-size: 17px; font-weight: bold; margin-bottom: 12px; margin-top: 20px; border-bottom: 1px solid #333; padding-bottom: 5px; display: flex; align-items: center; gap: 8px; }
.section-header:first-of-type { margin-top: 0; }
.form-pair { display: flex; align-items: center; margin-bottom: 12px; gap: 8px; }
.form-pair label { flex: 0 0 120px; color: var(--primary-blue); font-weight: bold; font-size: 14px; cursor: help; }
.form-input-wrapper { flex: 1; display: flex; align-items: center; gap: 6px; }

/* Unified Input Style */
.ptc-input { background: #111 !important; border: 1px solid #444 !important; border-radius: 4px !important; color: #fff !important; padding: 6px 10px !important; width: 100% !important; box-sizing: border-box !important; font-size: 14px !important; height: 32px !important; }
.ptc-input:focus { border-color: var(--primary-blue) !important; outline: none !important; }
.input-small { width: 70px !important; flex: 0 0 70px !important; }

.form-input-wrapper input[type="checkbox"] { accent-color: var(--primary-blue); width: 18px; height: 18px; cursor: pointer; }
.unit-label { font-size: 12px; color: #777; white-space: nowrap; }

#mapping_table { width: 100%; border-collapse: collapse; margin-top: 5px; }
#mapping_table th { text-align: left; color: var(--primary-blue); padding: 6px; border-bottom: 1px solid #333; font-size: 13px; }
#mapping_table td { padding: 4px 0; }

/* Unified Button Style */
.btn-ptc { background: transparent; color: var(--primary-blue); border: 1px solid var(--primary-blue); border-radius: 4px; cursor: pointer; transition: all 0.2s; font-weight: bold; }
.btn-ptc:hover { background: rgba(0, 170, 255, 0.1); }

.btn-save { padding: 10px 15px; font-size: 14px; width: 100%; margin-bottom: 20px; text-transform: uppercase; letter-spacing: 1px; }
.btn-add { padding: 6px 12px; font-size: 12px; margin-top: 10px; }

#ptc-log { background: #000; border: 1px solid #333; border-radius: 8px; color: #00ffaa; font-family: 'Courier New', monospace; font-size: 13px; padding: 15px; flex-grow: 1; overflow-y: auto; white-space: pre-wrap; word-break: break-all; margin-top: 10px; min-height: 600px; }
@media (max-width: 1250px) { #ptc-wrapper { flex-wrap: wrap; } .ptc-col { flex: 1 1 45%; } #ptc-col-log { flex: 1 1 100%; } }
</style>

<form method="post" autocomplete="off">
    <div id="ptc-wrapper">
        <!-- COL 1: SERVERS -->
        <div class="ptc-col" id="ptc-col-servers">
            <input type="submit" value="Save & Apply Settings" class="btn-ptc btn-save">
            
            <div class="section-header"><i class="fa fa-play-circle"></i> Plex Server</div>
            <div class="form-pair"><label title="Enable Plex session monitoring">Enable:</label><div class="form-input-wrapper"><input type="checkbox" name="ENABLE_PLEX" value="True" <?= $ptc_cfg['ENABLE_PLEX'] == 'True' ? 'checked' : '' ?> ></div></div>
            <div class="form-pair"><label title="URL of your Plex Server (e.g. http://192.168.1.100:32400)">URL:</label><div class="form-input-wrapper"><input type="text" name="PLEX_URL" value="<?= $ptc_cfg['PLEX_URL'] ?>" class="ptc-input"></div></div>
            <div class="form-pair"><label title="Your Plex X-Token">Token:</label><div class="form-input-wrapper"><input type="password" name="PLEX_TOKEN" value="<?= $ptc_cfg['PLEX_TOKEN'] ?>" class="ptc-input" onmouseover="this.type='text'" onmouseout="this.type='password'" autocomplete="new-password"></div></div>

            <div class="section-header"><i class="fa fa-server"></i> Emby Server</div>
            <div class="form-pair"><label title="Enable Emby session monitoring">Enable:</label><div class="form-input-wrapper"><input type="checkbox" name="ENABLE_EMBY" value="True" <?= $ptc_cfg['ENABLE_EMBY'] == 'True' ? 'checked' : '' ?> ></div></div>
            <div class="form-pair"><label title="URL of your Emby Server">URL:</label><div class="form-input-wrapper"><input type="text" name="EMBY_URL" value="<?= $ptc_cfg['EMBY_URL'] ?>" class="ptc-input"></div></div>
            <div class="form-pair"><label title="Your Emby API Key">API Key:</label><div class="form-input-wrapper"><input type="password" name="EMBY_API_KEY" value="<?= $ptc_cfg['EMBY_API_KEY'] ?>" class="ptc-input" autocomplete="new-password"></div></div>

            <div class="section-header"><i class="fa fa-film"></i> Jellyfin Server</div>
            <div class="form-pair"><label title="Enable Jellyfin session monitoring">Enable:</label><div class="form-input-wrapper"><input type="checkbox" name="ENABLE_JELLYFIN" value="True" <?= $ptc_cfg['ENABLE_JELLYFIN'] == 'True' ? 'checked' : '' ?> ></div></div>
            <div class="form-pair"><label title="URL of your Jellyfin Server">URL:</label><div class="form-input-wrapper"><input type="text" name="JELLYFIN_URL" value="<?= $ptc_cfg['JELLYFIN_URL'] ?>" class="ptc-input"></div></div>
            <div class="form-pair"><label title="Your Jellyfin API Key">API Key:</label><div class="form-input-wrapper"><input type="password" name="JELLYFIN_API_KEY" value="<?= $ptc_cfg['JELLYFIN_API_KEY'] ?>" class="ptc-input" autocomplete="new-password"></div></div>
        </div>

        <!-- COL 2: PATHS & TUNING -->
        <div class="ptc-col" id="ptc-col-tuning">
            <div class="section-header"><i class="fa fa-folder-open"></i> Storage Paths</div>
            <div class="form-pair"><label title="The root path of your array (usually /mnt/user)">Array Root:</label><div class="form-input-wrapper"><input type="text" name="ARRAY_ROOT" value="<?= $ptc_cfg['ARRAY_ROOT'] ?>" class="ptc-input"></div></div>
            <div class="form-pair"><label title="The root path of your cache pool (e.g. /mnt/cache)">Cache Root:</label><div class="form-input-wrapper"><input type="text" name="CACHE_ROOT" value="<?= $ptc_cfg['CACHE_ROOT'] ?>" class="ptc-input"></div></div>
            <div class="form-pair"><label title="Comma-separated list of directory names to ignore">Exclude:</label><div class="form-input-wrapper"><input type="text" name="EXCLUDE_DIRS" value="<?= $ptc_cfg['EXCLUDE_DIRS'] ?>" placeholder="temp,skip" class="ptc-input"></div></div>

            <div class="section-header"><i class="fa fa-exchange"></i> Docker Mappings</div>
            <table id="mapping_table"><thead><tr><th title="Path on your Unraid host">Host Path</th><th title="Path inside the Docker container">Docker Path</th><th></th></tr></thead><tbody></tbody></table>
            <button type="button" class="btn-ptc btn-add" onclick="addMappingRow()">+ Add Mapping</button>

            <div class="section-header"><i class="fa fa-cogs"></i> Tuning & Cleanup</div>
            <div class="form-pair"><label title="Seconds between stream checks">Interval:</label><div class="form-input-wrapper"><input type="number" name="CHECK_INTERVAL" value="<?= $ptc_cfg['CHECK_INTERVAL'] ?>" class="ptc-input input-small"><span class="unit-label">sec</span></div></div>
            <div class="form-pair"><label title="Wait this many seconds after stream starts before moving to cache">Copy Delay:</label><div class="form-input-wrapper"><input type="number" name="COPY_DELAY" value="<?= $ptc_cfg['COPY_DELAY'] ?>" class="ptc-input input-small"><span class="unit-label">sec</span></div></div>
            <div class="form-pair"><label title="Do not move files if cache usage is above this percentage">Max Cache:</label><div class="form-input-wrapper"><input type="number" name="CACHE_MAX_USAGE" value="<?= $ptc_cfg['CACHE_MAX_USAGE'] ?>" class="ptc-input input-small"><span class="unit-label">%</span></div></div>
            <div class="form-pair"><label title="Enable automatic removal of media from cache after watching">Smart Clean:</label><div class="form-input-wrapper"><input type="checkbox" name="ENABLE_SMART_CLEANUP" value="True" <?= $ptc_cfg['ENABLE_SMART_CLEANUP'] == 'True' ? 'checked' : '' ?> ></div></div>
            <div class="form-pair"><label title="Wait this many seconds after watching before deleting from cache">Del Delay:</label><div class="form-input-wrapper"><input type="number" name="MOVIE_DELETE_DELAY" value="<?= $ptc_cfg['MOVIE_DELETE_DELAY'] ?>" class="ptc-input input-small"><span class="unit-label">sec</span></div></div>
            <div class="form-pair"><label title="Keep this many previous episodes on the cache">Keep Previous:</label><div class="form-input-wrapper"><input type="number" name="EPISODE_KEEP_PREVIOUS" value="<?= $ptc_cfg['EPISODE_KEEP_PREVIOUS'] ?>" class="ptc-input input-small"><span class="unit-label">ep</span></div></div>
        </div>

        <!-- COL 3: LOG -->
        <div class="ptc-col" id="ptc-col-log">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <h3 style="margin:0; color:var(--primary-blue); font-size: 16px;"><i class="fa fa-terminal"></i> Live Log</h3>
                <div style="display:flex; align-items:center; gap:8px;">
                    <label style="color:#888; font-size:11px; cursor:pointer; display:flex; align-items:center; gap:4px;">
                        <input type="checkbox" id="auto_refresh" checked style="width:12px;height:12px;"> Auto
                    </label>
                    <button type="button" class="btn-ptc btn-add" onclick="refreshLog();" style="margin:0;">Refresh</button>
                </div>
            </div>
            <div id="ptc-log">Loading Logs...</div>
        </div>
    </div>
</form>

<script>
function refreshLog() { $.get('/plugins/plex_to_cache/get_log.php', function(data) { var logDiv = $('#ptc-log'); logDiv.text(data); logDiv.scrollTop(logDiv[0].scrollHeight); }); }
function addMappingRow(dockerVal = '', hostVal = '') {
    var table = document.getElementById('mapping_table').getElementsByTagName('tbody')[0];
    var row = table.insertRow(-1);
    var cell1 = row.insertCell(0); var cell2 = row.insertCell(1); var cell3 = row.insertCell(2);
    cell1.innerHTML = '<input type="text" name="mapping_host[]" value="' + hostVal + '" class="ptc-input" style="padding:4px !important;">';
    cell2.innerHTML = '<input type="text" name="mapping_docker[]" value="' + dockerVal + '" class="ptc-input" style="padding:4px !important;">';
    cell3.innerHTML = '<a href="#" onclick="deleteRow(this); return false;" style="color:#ff4444; font-size:16px; margin-left:5px;"><i class="fa fa-minus-circle"></i></a>';
}
function deleteRow(btn) { var row = btn.parentNode.parentNode; row.parentNode.removeChild(row); }
$(function() { <?php foreach ($mappings_pairs as $pair): ?> addMappingRow('<?= addslashes($pair[0]) ?>', '<?= addslashes($pair[1]) ?>'); <?php endforeach; ?> if (document.getElementById('mapping_table').rows.length <= 1) { addMappingRow(); } refreshLog(); setInterval(function() { if ($('#auto_refresh').is(':checked')) refreshLog(); }, 3000); });
</script>