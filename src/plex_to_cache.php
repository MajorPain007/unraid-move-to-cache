<?php
$ptc_plugin = "plex_to_cache";
$ptc_cfg_file = "/boot/config/plugins/$ptc_plugin/settings.cfg";
$ptc_log_file = "/var/log/plex_to_cache.log";

// Ajax Log Request
if (isset($_GET['action']) && $_GET['action'] == 'get_log') {
    if (file_exists($ptc_log_file)) {
        echo shell_exec("tail -n 200 " . escapeshellarg($ptc_log_file));
    } else {
        echo "Log file not found. Service might be starting...";
    }
    exit;
}

// Defaults
$ptc_cfg = [
    "LANGUAGE" => "EN",
    "ENABLE_PLEX" => "False", "PLEX_URL" => "http://localhost:32400", "PLEX_TOKEN" => "",
    "ENABLE_EMBY" => "False", "EMBY_URL" => "http://localhost:8096", "EMBY_API_KEY" => "",
    "ENABLE_JELLYFIN" => "False", "JELLYFIN_URL" => "http://localhost:8096", "JELLYFIN_API_KEY" => "",
    "CHECK_INTERVAL" => "10", "CACHE_MAX_USAGE" => "80", "COPY_DELAY" => "30",
    "ENABLE_SMART_CLEANUP" => "False", "MOVIE_DELETE_DELAY" => "1800", "EPISODE_KEEP_PREVIOUS" => "2",
    "EXCLUDE_DIRS" => "", "MEDIA_FILETYPES" => ".mkv .mp4 .avi", "ARRAY_ROOT" => "/mnt/user",
    "CACHE_ROOT" => "/mnt/cache", "DOCKER_MAPPINGS" => ""
];

// Load Config
if (file_exists($ptc_cfg_file)) {
    $ptc_loaded = parse_ini_file($ptc_cfg_file);
    if ($ptc_loaded) {
        $ptc_cfg = array_merge($ptc_cfg, $ptc_loaded);
    }
}

// Translations
$lang = isset($ptc_cfg['LANGUAGE']) && $ptc_cfg['LANGUAGE'] == 'DE' ? 'DE' : 'EN';
$txt = [
    'DE' => [
        'settings' => 'Einstellungen', 'save' => 'Speichern & Übernehmen', 'plex' => 'Plex Media Server',
        'emby' => 'Emby Server', 'jelly' => 'Jellyfin Server', 'enable' => 'Aktivieren:',
        'paths' => 'Pfad Konfiguration', 'mappings' => 'Docker Pfad Mappings', 'tuning' => 'Tuning & Cleanup',
        'interval' => 'Check Intervall:', 'delay' => 'Kopier Verzögerung:', 'max_cache' => 'Max Cache:',
        'smart' => 'Smart Cleanup:', 'del_delay' => 'Lösch Verzögerung:', 'keep' => 'Episoden behalten:',
        'host' => 'Host Pfad (Unraid)', 'docker' => 'Docker Pfad (Container)', 'log' => 'Log Auswertung',
        'lang' => 'Sprache / Language', 'exclude' => 'Ordner ausschließen:', 'filetypes' => 'Dateitypen:',
        'status_saved' => 'Einstellungen gespeichert & Dienst neugestartet',
        'unit_sec' => 'sek', 'unit_percent' => '%', 'unit_count' => 'Anzahl'
    ],
    'EN' => [
        'settings' => 'Settings', 'save' => 'Save & Apply', 'plex' => 'Plex Media Server',
        'emby' => 'Emby Server', 'jelly' => 'Jellyfin Server', 'enable' => 'Enable:',
        'paths' => 'Storage Paths', 'mappings' => 'Docker Path Mappings', 'tuning' => 'Tuning & Cleanup',
        'interval' => 'Check Interval:', 'delay' => 'Copy Delay:', 'max_cache' => 'Max Cache:',
        'smart' => 'Smart Cleanup:', 'del_delay' => 'Delete Delay:', 'keep' => 'Keep Episodes:',
        'host' => 'Host Path (Unraid)', 'docker' => 'Docker Path (Container)', 'log' => 'Log Output',
        'lang' => 'Language', 'exclude' => 'Exclude Dirs:', 'filetypes' => 'File Types:',
        'status_saved' => 'Settings Saved & Service Restarted',
        'unit_sec' => 'sec', 'unit_percent' => '%', 'unit_count' => 'count'
    ]
];

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
    
    // Process Docker Mappings (Stored as docker:host internally)
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
    echo "<script>window.location.reload();</script>";
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
:root { --primary-blue: #00aaff; --bg-dark: #111; }
#ptc-wrapper { display: flex; flex-wrap: nowrap; align-items: flex-start; justify-content: space-between; gap: 20px; width: 100%; box-sizing: border-box; padding: 10px 0; }
#ptc-settings, #ptc-log-container { background: var(--bg-dark); border-radius: 12px; box-shadow: 0 0 12px rgba(0, 170, 255, 0.2); color: #f0f8ff; padding: 20px; box-sizing: border-box; }
#ptc-settings { flex: 0 0 48%; }
#ptc-log-container { flex: 0 0 50%; display: flex; flex-direction: column; min-height: 850px; }
.section-header { color: var(--primary-blue); font-size: 18px; font-weight: bold; margin-bottom: 15px; margin-top: 25px; border-bottom: 1px solid #333; padding-bottom: 5px; }
.section-header:first-of-type { margin-top: 0; }
.form-pair { display: flex; align-items: center; margin-bottom: 15px; gap: 10px; }
.form-pair label { flex: 0 0 140px; color: var(--primary-blue); font-weight: bold; font-size: 14px; }
.form-input-wrapper { flex: 1; display: flex; align-items: center; }
.form-input-wrapper input[type="text"], .form-input-wrapper input[type="password"], .form-input-wrapper input[type="number"], .form-input-wrapper select { background: #111; border: 1px solid var(--primary-blue); border-radius: 5px; color: #fff; padding: 8px; width: 100%; box-sizing: border-box; }
.form-input-wrapper input[type="checkbox"] { accent-color: var(--primary-blue); width: 18px; height: 18px; cursor: pointer; }
.help-text { font-size: 12px; color: #888; margin-left: 8px; font-style: italic; }
#mapping_table { width: 100%; border-collapse: collapse; margin-top: 10px; }
#mapping_table th { text-align: left; color: var(--primary-blue); padding: 8px; border-bottom: 1px solid #333; font-size: 13px; }
#mapping_table td { padding: 5px; }
.btn-save { background: var(--primary-blue); color: #fff; border: none; border-radius: 4px; padding: 10px 20px; cursor: pointer; font-weight: bold; font-size: 14px; }
.btn-save:hover { filter: brightness(1.1); }
.btn-add { background: transparent; color: var(--primary-blue); border: 1px solid var(--primary-blue); padding: 5px 10px; border-radius: 4px; cursor: pointer; margin-top: 10px; }
.btn-add:hover { background: rgba(0, 170, 255, 0.1); }
#ptc-log { background: #000; border: 1px solid var(--primary-blue); border-radius: 8px; color: #00ffaa; font-family: monospace; font-size: 12px; padding: 15px; height: 750px; overflow-y: scroll; white-space: pre-wrap; word-break: break-all; }
@media (max-width: 1100px) { #ptc-wrapper { flex-wrap: wrap; } #ptc-settings, #ptc-log-container { flex: 1 1 100%; } }
</style>

<div id="ptc-wrapper">
    <div id="ptc-settings">
        <form method="post" autocomplete="off">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
                <h2 style="margin:0; color:var(--primary-blue);"><?= $txt[$lang]['settings'] ?></h2>
                <input type="submit" value="<?= $txt[$lang]['save'] ?>" class="btn-save">
            </div>

            <div class="form-pair"><label><?= $txt[$lang]['lang'] ?>:</label><div class="form-input-wrapper"><select name="LANGUAGE" onchange="this.form.submit()"><option value="EN" <?= $lang == 'EN' ? 'selected' : '' ?>>English</option><option value="DE" <?= $lang == 'DE' ? 'selected' : '' ?>>Deutsch</option></select></div></div>

            <div class="section-header"><i class="fa fa-play-circle"></i> <?= $txt[$lang]['plex'] ?></div>
            <div class="form-pair"><label><?= $txt[$lang]['enable'] ?></label><div class="form-input-wrapper"><input type="checkbox" name="ENABLE_PLEX" value="True" <?= $ptc_cfg['ENABLE_PLEX'] == 'True' ? 'checked' : '' ?> ></div></div>
            <div class="form-pair"><label>URL:</label><div class="form-input-wrapper"><input type="text" name="PLEX_URL" value="<?= $ptc_cfg['PLEX_URL'] ?>" autocomplete="off"></div></div>
            <div class="form-pair"><label>Token:</label><div class="form-input-wrapper"><input type="password" name="PLEX_TOKEN" value="<?= $ptc_cfg['PLEX_TOKEN'] ?>" onmouseover="this.type='text'" onmouseout="this.type='password'" autocomplete="new-password"></div></div>

            <div class="section-header"><i class="fa fa-server"></i> <?= $txt[$lang]['emby'] ?></div>
            <div class="form-pair"><label><?= $txt[$lang]['enable'] ?></label><div class="form-input-wrapper"><input type="checkbox" name="ENABLE_EMBY" value="True" <?= $ptc_cfg['ENABLE_EMBY'] == 'True' ? 'checked' : '' ?> ></div></div>
            <div class="form-pair"><label>URL:</label><div class="form-input-wrapper"><input type="text" name="EMBY_URL" value="<?= $ptc_cfg['EMBY_URL'] ?>" autocomplete="off"></div></div>
            <div class="form-pair"><label>API Key:</label><div class="form-input-wrapper"><input type="password" name="EMBY_API_KEY" value="<?= $ptc_cfg['EMBY_API_KEY'] ?>" onmouseover="this.type='text'" onmouseout="this.type='password'" autocomplete="new-password"></div></div>

            <div class="section-header"><i class="fa fa-film"></i> <?= $txt[$lang]['jelly'] ?></div>
            <div class="form-pair"><label><?= $txt[$lang]['enable'] ?></label><div class="form-input-wrapper"><input type="checkbox" name="ENABLE_JELLYFIN" value="True" <?= $ptc_cfg['ENABLE_JELLYFIN'] == 'True' ? 'checked' : '' ?> ></div></div>
            <div class="form-pair"><label>URL:</label><div class="form-input-wrapper"><input type="text" name="JELLYFIN_URL" value="<?= $ptc_cfg['JELLYFIN_URL'] ?>" autocomplete="off"></div></div>
            <div class="form-pair"><label>API Key:</label><div class="form-input-wrapper"><input type="password" name="JELLYFIN_API_KEY" value="<?= $ptc_cfg['JELLYFIN_API_KEY'] ?>" onmouseover="this.type='text'" onmouseout="this.type='password'" autocomplete="new-password"></div></div>

            <div class="section-header"><i class="fa fa-folder-open"></i> <?= $txt[$lang]['paths'] ?></div>
            <div class="form-pair"><label><?= $txt[$lang]['host'] ?>:</label><div class="form-input-wrapper"><input type="text" name="ARRAY_ROOT" value="<?= $ptc_cfg['ARRAY_ROOT'] ?>"></div></div>
            <div class="form-pair"><label>Cache Root:</label><div class="form-input-wrapper"><input type="text" name="CACHE_ROOT" value="<?= $ptc_cfg['CACHE_ROOT'] ?>"></div></div>
            <div class="form-pair"><label><?= $txt[$lang]['exclude'] ?></label><div class="form-input-wrapper"><input type="text" name="EXCLUDE_DIRS" value="<?= $ptc_cfg['EXCLUDE_DIRS'] ?>"></div></div>

            <div class="section-header"><i class="fa fa-exchange"></i> <?= $txt[$lang]['mappings'] ?></div>
            <table id="mapping_table"><thead><tr><th style="width: 45%;"><?= $txt[$lang]['host'] ?></th><th style="width: 45%;"><?= $txt[$lang]['docker'] ?></th><th style="width: 10%;"></th></tr></thead><tbody></tbody></table>
            <button type="button" class="btn-add" onclick="addMappingRow()">+ Mapping</button>

            <div class="section-header"><i class="fa fa-cogs"></i> <?= $txt[$lang]['tuning'] ?></div>
            <div class="form-pair"><label><?= $txt[$lang]['interval'] ?></label><div class="form-input-wrapper"><input type="number" name="CHECK_INTERVAL" value="<?= $ptc_cfg['CHECK_INTERVAL'] ?>" style="width: 80px;"><span class="help-text"><?= $txt[$lang]['unit_sec'] ?></span></div></div>
            <div class="form-pair"><label><?= $txt[$lang]['delay'] ?></label><div class="form-input-wrapper"><input type="number" name="COPY_DELAY" value="<?= $ptc_cfg['COPY_DELAY'] ?>" style="width: 80px;"><span class="help-text"><?= $txt[$lang]['unit_sec'] ?></span></div></div>
            <div class="form-pair"><label><?= $txt[$lang]['max_cache'] ?></label><div class="form-input-wrapper"><input type="number" name="CACHE_MAX_USAGE" value="<?= $ptc_cfg['CACHE_MAX_USAGE'] ?>" style="width: 80px;"><span class="help-text"><?= $txt[$lang]['unit_percent'] ?></span></div></div>
            <div class="form-pair"><label><?= $txt[$lang]['smart'] ?></label><div class="form-input-wrapper"><input type="checkbox" name="ENABLE_SMART_CLEANUP" value="True" <?= $ptc_cfg['ENABLE_SMART_CLEANUP'] == 'True' ? 'checked' : '' ?> ></div></div>
            <div class="form-pair"><label><?= $txt[$lang]['del_delay'] ?></label><div class="form-input-wrapper"><input type="number" name="MOVIE_DELETE_DELAY" value="<?= $ptc_cfg['MOVIE_DELETE_DELAY'] ?>" style="width: 80px;"><span class="help-text"><?= $txt[$lang]['unit_sec'] ?></span></div></div>
            <div class="form-pair"><label><?= $txt[$lang]['keep'] ?></label><div class="form-input-wrapper"><input type="number" name="EPISODE_KEEP_PREVIOUS" value="<?= $ptc_cfg['EPISODE_KEEP_PREVIOUS'] ?>" style="width: 80px;"></div></div>
        </form>
    </div>

    <div id="ptc-log-container">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 15px;">
            <h3 style="margin:0; color:var(--primary-blue);"><i class="fa fa-terminal"></i> <?= $txt[$lang]['log'] ?></h3>
            <div style="display:flex; align-items:center; gap:10px;">
                <label style="color:#888; font-size:12px; cursor:pointer; display:flex; align-items:center; gap:5px;">
                    <input type="checkbox" id="auto_refresh" checked style="width:14px;height:14px;"> Auto Refresh
                </label>
                <button class="btn-add" onclick="refreshLog();" style="margin:0;">Refresh</button>
            </div>
        </div>
        <div id="ptc-log">Loading Logs...</div>
    </div>
</div>

<script>
function refreshLog() { $.get('/plugins/plex_to_cache/get_log.php', function(data) { var logDiv = $('#ptc-log'); logDiv.text(data); logDiv.scrollTop(logDiv[0].scrollHeight); }); }
function addMappingRow(dockerVal = '', hostVal = '') {
    var table = document.getElementById('mapping_table').getElementsByTagName('tbody')[0];
    var row = table.insertRow(-1);
    var cell1 = row.insertCell(0); var cell2 = row.insertCell(1); var cell3 = row.insertCell(2);
    cell1.innerHTML = '<input type="text" name="mapping_host[]" value="' + hostVal + '" style="width:100%; background:#111; border:1px solid var(--primary-blue); color:#fff; padding:6px; border-radius:4px;">';
    cell2.innerHTML = '<input type="text" name="mapping_docker[]" value="' + dockerVal + '" style="width:100%; background:#111; border:1px solid var(--primary-blue); color:#fff; padding:6px; border-radius:4px;">';
    cell3.innerHTML = '<a href="#" onclick="deleteRow(this); return false;" style="color:#ff4444; font-size:18px; display:block; text-align:center;"><i class="fa fa-minus-circle"></i></a>';
}
function deleteRow(btn) { var row = btn.parentNode.parentNode; row.parentNode.removeChild(row); }
$(function() {
    <?php foreach ($mappings_pairs as $pair): ?> addMappingRow('<?= addslashes($pair[0]) ?>', '<?= addslashes($pair[1]) ?>'); <?php endforeach; ?>
    if (document.getElementById('mapping_table').rows.length <= 1) { addMappingRow(); }
    refreshLog(); setInterval(function() { if ($('#auto_refresh').is(':checked')) refreshLog(); }, 3000);
});
</script>