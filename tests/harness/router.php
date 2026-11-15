<?php

$src = dirname(__DIR__, 2) . '/src';
$uri = parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH);
$token = 'harness-token';

if ($uri === '/plugins/plex_to_cache/include/api.php') {
    if ($_SERVER['REQUEST_METHOD'] === 'POST' && ($_POST['csrf_token'] ?? '') !== $token) {
        exit;
    }
    require "$src/include/api.php";
    return true;
}
if (strpos($uri, '/plugins/plex_to_cache/assets/') === 0) {
    $dist = dirname($src) . '/web/dist';
    $file = realpath($dist . '/' . substr($uri, strlen('/plugins/plex_to_cache/assets/')));
    if ($file === false || strpos($file, realpath($dist)) !== 0) {
        http_response_code(404);
        return true;
    }
    header('Content-Type: ' . (substr($file, -4) === '.css' ? 'text/css' : 'application/javascript'));
    header('Cache-Control: no-store');
    readfile($file);
    return true;
}
if (strpos($uri, '/unraid/') === 0) {
    $file = __DIR__ . '/.unraid/' . basename($uri);
    if (!is_file($file)) {
        http_response_code(404);
        return true;
    }
    $types = array('css' => 'text/css', 'woff' => 'font/woff');
    header('Content-Type: ' . ($types[pathinfo($file, PATHINFO_EXTENSION)] ?? 'application/octet-stream'));
    readfile($file);
    return true;
}
if ($uri !== '/' && $uri !== '/index.php') {
    http_response_code(404);
    return true;
}

function autov($path)
{
    echo $path . '?v=' . time();
}

$var = array('csrf_token' => $token);
$theme = preg_replace('/[^a-z]/', '', $_GET['theme'] ?? 'black');
$themes = array(
    'black' => array('#121212', '#f2f2f2', '#1c1b1b', '#2b2b2b', '#a8a8a8'),
    'white' => array('#f2f2f2', '#1c1b1b', '#ffffff', '#e3e3e3', '#606e7f'),
    'azure' => array('#e4e2e4', '#606e7f', '#f7f9f9', '#d8d8d8', '#8b98a8'),
    'gray'  => array('#1c1b1b', '#e4e4e4', '#2b2b2b', '#3a3a3a', '#a6a6a6'),
);
$t = $themes[$theme] ?? $themes['black'];
$real = is_file(__DIR__ . '/.unraid/default-base.css');
?><!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Move to Cache - harness</title>
<?php if ($real): ?>
<style>
  @font-face { font-family: clear-sans; src: url(/unraid/clear-sans.woff) format('woff'); }
  @font-face { font-family: clear-sans; font-weight: bold; src: url(/unraid/clear-sans-bold.woff) format('woff'); }
</style>
<link rel="stylesheet" href="/unraid/default-color-palette.css">
<link rel="stylesheet" href="/unraid/theme-<?=$theme?>.css">
<link rel="stylesheet" href="/unraid/default-base.css">
<link rel="stylesheet" href="/unraid/default-dynamix.css">
<link rel="stylesheet" href="/unraid/font-awesome.css">
<style>
  #header { height: 64px; display: flex; align-items: center; gap: 24px; padding: 0 24px; background: #1c1b1b; color: #f2f2f2; border-bottom: 3px solid #ff8c2f; }
  #header b { font-size: 2rem; letter-spacing: .1em; }
  #header a { color: #f2f2f2; text-decoration: none; font-size: 1.3rem; text-transform: uppercase; opacity: .8; }
  #header a.on { opacity: 1; border-bottom: 2px solid #ff8c2f; }
  #displaybox { padding: 20px 28px; }
  .title { font-size: 1.6rem; text-transform: uppercase; letter-spacing: .1em; margin: 0 0 18px; padding-bottom: 8px; border-bottom: 1px solid var(--border-color); }
</style>
<?php else: ?>
<style>
  html { font-size: 62.5%; }
  :root {
    --text-color: <?=$t[1]?>;
    --mild-background-color: <?=$t[2]?>;
    --border-color: <?=$t[3]?>;
    --alt-text-color: <?=$t[4]?>;
  }
  body { margin: 0; background: <?=$t[0]?>; color: <?=$t[1]?>; font-family: "clear-sans", "Segoe UI", Arial, sans-serif; font-size: 1.3rem; }
  #header { height: 64px; display: flex; align-items: center; gap: 24px; padding: 0 24px; background: #1c1b1b; color: #f2f2f2; border-bottom: 3px solid #ff8c2f; }
  #header b { font-size: 2rem; letter-spacing: .1em; }
  #header a { color: #f2f2f2; text-decoration: none; font-size: 1.3rem; text-transform: uppercase; opacity: .8; }
  #header a.on { opacity: 1; border-bottom: 2px solid #ff8c2f; }
  #displaybox { padding: 20px 28px; }
  .title { font-size: 1.6rem; text-transform: uppercase; letter-spacing: .1em; margin: 0 0 18px; padding-bottom: 8px; border-bottom: 1px solid <?=$t[3]?>; }
  input[type=button], input[type=submit], button, a.button {
    font-family: clear-sans, sans-serif; font-size: 1.1rem; font-weight: bold; letter-spacing: 1.8px;
    text-transform: uppercase; min-width: 86px; margin: 10px 12px 10px 0; padding: 8px; border: 0; border-radius: 4px;
    color: #ff8c2f; background: linear-gradient(90deg, #e22828 0, #ff8c2f) 0 0 no-repeat, linear-gradient(90deg, #e22828 0, #ff8c2f) 0 100% no-repeat;
    background-size: 100% 2px; cursor: pointer;
  }
  input[type=text], input[type=number], input[type=password], select, textarea {
    font-family: clear-sans, sans-serif; font-size: 1.3rem; background: transparent; border: none;
    border-bottom: 1px solid #e3e3e3; padding: 5px 6px; min-height: 2rem; width: 300px; margin: 0 20px 0 0;
    border-radius: 0; color: inherit;
  }
  #footer { position: fixed; bottom: 0; left: 0; right: 0; z-index: 10000; display: flex; justify-content: space-between;
            padding: 6px 16px; background: #000; color: #a8a8a8; font-size: 1.2rem; }
  table { border-collapse: collapse; }
  table tbody td { padding: 4px 8px; }
  label { cursor: default; }
</style>
<?php endif; ?>
</head>
<body>
<div id="header"><b>UNRAID</b>
  <?php foreach (array_keys($themes) as $name): ?>
  <a href="?theme=<?=$name?>" class="<?=$name === $theme ? 'on' : ''?>"><?=$name?></a>
  <?php endforeach; ?>
</div>
<div id="displaybox">
  <div class="title">Move to Cache</div>
  <?php include "$src/include/page.php"; ?>
</div>
<div id="footer"><span class="footer-left">Array Started</span><span class="footer-spacer"></span>
  <span>Unraid&reg; webGui &copy;2026, Lime Technology, Inc. <a href="#">manual</a></span></div>
</body>
</html>
