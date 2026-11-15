<?php
$mtcToken = isset($var['csrf_token']) ? $var['csrf_token'] : '';
$mtcBase = '/plugins/plex_to_cache/assets';
?>
<link rel="stylesheet" href="<?php autov("$mtcBase/app.css"); ?>">
<div id="mtc-app" class="mtc" data-csrf="<?=htmlspecialchars($mtcToken, ENT_QUOTES)?>"
     data-api="/plugins/plex_to_cache/include/api.php">
  <div class="mtc-boot">Loading&hellip;</div>
</div>
<script src="<?php autov("$mtcBase/app.js"); ?>"></script>
