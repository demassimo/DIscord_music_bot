<?php
require __DIR__ . '/vendor/autoload.php';

use Ratchet\MessageComponentInterface;
use Ratchet\ConnectionInterface;
use Ratchet\Http\HttpServer;
use Ratchet\WebSocket\WsServer;
use Ratchet\Server\IoServer;

class Control implements MessageComponentInterface {
    protected $clients;
    public function __construct() {
        $this->clients = new \SplObjectStorage;
    }
    public function onOpen(ConnectionInterface $conn) {
        $this->clients->attach($conn);
    }
    public function onMessage(ConnectionInterface $from, $msg) {
        foreach ($this->clients as $client) {
            if ($from !== $client) {
                $client->send($msg);
            }
        }
    }
    public function onClose(ConnectionInterface $conn) {
        $this->clients->detach($conn);
    }
    public function onError(ConnectionInterface $conn, \Exception $e) {
        $conn->close();
    }
}

$botProcess = proc_open('python bot.py', [
    1 => ['pipe', 'w'],
    2 => ['pipe', 'w'],
], $pipes, __DIR__);

if (!is_resource($botProcess)) {
    echo "Failed to start bot\n";
} else {
    register_shutdown_function(function() use ($botProcess) {
        proc_terminate($botProcess);
    });
}

$server = IoServer::factory(
    new HttpServer(
        new WsServer(
            new Control()
        )
    ),
    8080
);

$server->run();
?>
