<?php
$mtcBin = getenv('MTC_BIN') ?: dirname(__DIR__) . '/bin/move-to-cache';

header('Content-Type: application/json');
header('Cache-Control: no-store');
if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
    echo json_encode(array('ok' => false, 'error' => 'POST only'));
    exit;
}
$action = (string)($_POST['action'] ?? '');
$params = $_POST;
unset($params['action'], $params['csrf_token']);
$input = json_encode($params, JSON_UNESCAPED_SLASHES | JSON_INVALID_UTF8_SUBSTITUTE);

$proc = proc_open(array($mtcBin, 'api', $action), array(0 => array('pipe', 'r'), 1 => array('pipe', 'w'), 2 => array('file', '/dev/null', 'w')), $pipes);
if (!is_resource($proc)) {
    echo json_encode(array('ok' => false, 'error' => 'The plugin program could not be started.'));
    exit;
}
fwrite($pipes[0], $input);
fclose($pipes[0]);
$json = stream_get_contents($pipes[1]);
fclose($pipes[1]);
proc_close($proc);
echo $json !== '' ? $json : json_encode(array('ok' => false, 'error' => 'The plugin program gave no answer.'));
