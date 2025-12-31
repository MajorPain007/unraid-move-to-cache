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
    foreach ($ptc_cfg as $key => $val) { $content .= "$key=\\