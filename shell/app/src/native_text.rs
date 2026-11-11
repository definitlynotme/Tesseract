//! Shell-native strings (shown before the WebView and the page catalogs load). All nine locales
//! are authoritative (T-P5-01, A16: maintained by hand here, terms aligned with ui/src/i18n).
pub const LOCALES:[&str;9]=["zh_CN","en_US","de_DE","es_ES","fr_FR","ja_JP","ko_KR","pt_BR","ru_RU"];
pub struct Message {pub id:&'static str,pub text:[&'static str;9]}
pub const MESSAGES:&[Message]=&[
    Message{id:"title",text:["Tesseract","Tesseract","Tesseract","Tesseract","Tesseract","Tesseract","Tesseract","Tesseract","Tesseract"]},
    Message{id:"instance_unknown",text:[
        "Tesseract 已在以其他权限运行，或无法确认实例状态。请检查已有实例及权限后重试。",
        "Tesseract is already running with different permissions, or its instance status could not be confirmed. Check existing instances and permissions, then try again.",
        "Tesseract läuft bereits mit anderen Berechtigungen, oder der Instanzstatus konnte nicht bestätigt werden. Prüfen Sie die laufenden Instanzen und die Berechtigungen und versuchen Sie es erneut.",
        "Tesseract ya se está ejecutando con otros permisos, o no se ha podido confirmar el estado de la instancia. Comprueba las instancias abiertas y los permisos, y vuelve a intentarlo.",
        "Tesseract est déjà en cours d’exécution avec d’autres autorisations, ou l’état de l’instance n’a pas pu être confirmé. Vérifiez les instances ouvertes et les autorisations, puis réessayez.",
        "Tesseract が別の権限で既に実行されているか、インスタンスの状態を確認できません。実行中のインスタンスと権限を確認してから、もう一度お試しください。",
        "Tesseract가 다른 권한으로 이미 실행 중이거나 인스턴스 상태를 확인할 수 없습니다. 실행 중인 인스턴스와 권한을 확인한 후 다시 시도하세요.",
        "O Tesseract já está em execução com outras permissões, ou não foi possível confirmar o estado da instância. Verifique as instâncias abertas e as permissões e tente novamente.",
        "Tesseract уже запущен с другими правами, или не удалось подтвердить состояние экземпляра. Проверьте запущенные экземпляры и права и повторите попытку."]},
    Message{id:"quitting_timeout",text:[
        "Tesseract 仍在退出。等待已超过 30 秒，请稍后重试。",
        "Tesseract is still quitting. The 30-second wait has expired. Please try again later.",
        "Tesseract wird noch beendet. Die Wartezeit von 30 Sekunden ist überschritten. Versuchen Sie es später erneut.",
        "Tesseract todavía se está cerrando. Se ha superado la espera de 30 segundos. Vuelve a intentarlo más tarde.",
        "Tesseract est toujours en cours de fermeture. L’attente a dépassé 30 secondes. Réessayez plus tard.",
        "Tesseract はまだ終了処理中です。待機時間が 30 秒を超えました。しばらくしてからもう一度お試しください。",
        "Tesseract가 아직 종료 중입니다. 대기 시간이 30초를 넘었습니다. 잠시 후 다시 시도하세요.",
        "O Tesseract ainda está sendo encerrado. A espera passou de 30 segundos. Tente novamente mais tarde.",
        "Tesseract ещё завершает работу. Ожидание превысило 30 секунд. Повторите попытку позже."]},
    Message{id:"runtime_timeout",text:[
        "Tesseract 的后台服务仍在收尾。等待已超过 30 秒，请稍后重试。",
        "Tesseract's background service is still cleaning up. The 30-second wait has expired. Please try again later.",
        "Der Hintergrunddienst von Tesseract räumt noch auf. Die Wartezeit von 30 Sekunden ist überschritten. Versuchen Sie es später erneut.",
        "El servicio en segundo plano de Tesseract todavía está terminando de cerrarse. Se ha superado la espera de 30 segundos. Vuelve a intentarlo más tarde.",
        "Le service en arrière-plan de Tesseract n’a pas encore fini son nettoyage. L’attente a dépassé 30 secondes. Réessayez plus tard.",
        "Tesseract のバックグラウンドサービスはまだ後処理中です。待機時間が 30 秒を超えました。しばらくしてからもう一度お試しください。",
        "Tesseract의 백그라운드 서비스가 아직 정리 중입니다. 대기 시간이 30초를 넘었습니다. 잠시 후 다시 시도하세요.",
        "O serviço em segundo plano do Tesseract ainda está concluindo a limpeza. A espera passou de 30 segundos. Tente novamente mais tarde.",
        "Фоновая служба Tesseract ещё выполняет очистку. Ожидание превысило 30 секунд. Повторите попытку позже."]},
    // P3-03 tray. Tier codenames stay English in every locale and stand alone, with no role word (DESIGN §6.11, F-03).
    Message{id:"tray_show",text:["显示 Tesseract","Show Tesseract","Tesseract anzeigen","Mostrar Tesseract","Afficher Tesseract","Tesseract を表示","Tesseract 표시","Mostrar Tesseract","Показать Tesseract"]},
    Message{id:"tray_quit",text:["退出","Quit","Beenden","Salir","Quitter","終了","종료","Sair","Выйти"]},
    Message{id:"tray_tier_line",text:["当前层级：{tier}","Current tier: {tier}","Aktuelle Stufe: {tier}","Nivel actual: {tier}","Niveau actuel\u{a0}: {tier}","現在の階層：{tier}","현재 계층: {tier}","Camada atual: {tier}","Текущий уровень: {tier}"]},
    // Only ever the {tier} value of tray_tier_line ("label: value"), so lowercase where the language writes it so
    // (en too: "Current tier: offline", T-P5-09).
    Message{id:"tray_offline",text:["离线","offline","offline","sin conexión","hors ligne","オフライン","오프라인","offline","не в сети"]},
    Message{id:"tray_core_running",text:["代理核心 运行中","Proxy core running","Proxy-Kern läuft","Núcleo del proxy en ejecución","Noyau du proxy en marche","プロキシコア 実行中","프록시 코어 실행 중","Núcleo do proxy em execução","Прокси-ядро работает"]},
    Message{id:"tray_core_stopped",text:["代理核心 已停止","Proxy core stopped","Proxy-Kern gestoppt","Núcleo del proxy detenido","Noyau du proxy arrêté","プロキシコア 停止","프록시 코어 중지됨","Núcleo do proxy parado","Прокси-ядро остановлено"]},
    // P3-04 quit, session end and crash dialogs.
    Message{id:"quit_unsaved",text:[
        "有 {count} 项更改没保存，仍然退出？",
        "{count} change(s) have not been saved. Quit anyway?",
        "Ungespeicherte Änderungen: {count}. Trotzdem beenden?",
        "Cambios sin guardar: {count}. ¿Salir de todos modos?",
        "Modifications non enregistrées\u{a0}: {count}. Quitter quand même\u{a0}?",
        "未保存の変更が {count} 件あります。それでも終了しますか？",
        "저장하지 않은 변경 사항이 {count}개 있습니다. 그래도 종료하시겠습니까?",
        "Alterações não salvas: {count}. Sair mesmo assim?",
        "Несохранённых изменений: {count}. Всё равно выйти?"]},
    Message{id:"quit_stall",text:[
        "Tesseract 正在恢复网络设置并退出，但已经 20 秒没有新的进度。",
        "Tesseract is restoring network settings and quitting, but there has been no progress for 20 seconds.",
        "Tesseract stellt die Netzwerkeinstellungen wieder her und wird beendet, aber seit 20 Sekunden gibt es keinen neuen Fortschritt.",
        "Tesseract está restaurando la configuración de red y cerrándose, pero lleva 20 segundos sin avanzar.",
        "Tesseract restaure les paramètres réseau et se ferme, mais rien n’a progressé depuis 20 secondes.",
        "Tesseract はネットワーク設定を復元して終了しようとしていますが、20 秒間進捗がありません。",
        "Tesseract가 네트워크 설정을 복원하고 종료하는 중이지만 20초 동안 진행이 없습니다.",
        "O Tesseract está restaurando as configurações de rede e sendo encerrado, mas não há progresso há 20 segundos.",
        "Tesseract восстанавливает сетевые настройки и завершает работу, но уже 20 секунд нет прогресса."]},
    Message{id:"quit_stall_scripts",text:[
        "如果强制退出，可以运行以下脚本手动恢复网络设置（unkill.bat 需以管理员身份运行）：\n{dir}\\restore_proxy.bat\n{dir}\\unkill.bat",
        "If you force quit, you can restore network settings manually with these scripts (run unkill.bat as administrator):\n{dir}\\restore_proxy.bat\n{dir}\\unkill.bat",
        "Wenn Sie das Beenden erzwingen, können Sie die Netzwerkeinstellungen mit diesen Skripten manuell wiederherstellen (führen Sie unkill.bat als Administrator aus):\n{dir}\\restore_proxy.bat\n{dir}\\unkill.bat",
        "Si fuerzas el cierre, puedes restaurar la configuración de red manualmente con estos scripts (ejecuta unkill.bat como administrador):\n{dir}\\restore_proxy.bat\n{dir}\\unkill.bat",
        "Si vous forcez la fermeture, vous pouvez restaurer les paramètres réseau manuellement avec ces scripts (exécutez unkill.bat en tant qu’administrateur)\u{a0}:\n{dir}\\restore_proxy.bat\n{dir}\\unkill.bat",
        "強制終了した場合は、次のスクリプトを実行するとネットワーク設定を手動で復元できます（unkill.bat は管理者として実行してください）：\n{dir}\\restore_proxy.bat\n{dir}\\unkill.bat",
        "강제로 종료하면 다음 스크립트로 네트워크 설정을 직접 복원할 수 있습니다(unkill.bat은 관리자 권한으로 실행하세요):\n{dir}\\restore_proxy.bat\n{dir}\\unkill.bat",
        "Se você forçar o encerramento, poderá restaurar as configurações de rede manualmente com estes scripts (execute unkill.bat como administrador):\n{dir}\\restore_proxy.bat\n{dir}\\unkill.bat",
        "Если выйти принудительно, сетевые настройки можно восстановить вручную этими скриптами (unkill.bat запускайте от имени администратора):\n{dir}\\restore_proxy.bat\n{dir}\\unkill.bat"]},
    Message{id:"quit_stall_wait",text:["继续等待","Keep waiting","Weiter warten","Seguir esperando","Continuer à attendre","引き続き待機","계속 기다리기","Continuar aguardando","Продолжить ожидание"]},
    Message{id:"quit_stall_force",text:[
        "强制退出（网络设置可能没恢复）","Force quit (network settings may not be restored)",
        "Beenden erzwingen (Netzwerk evtl. nicht wiederhergestellt)","Forzar cierre (puede que la red no se restaure)",
        "Forcer la fermeture (le réseau risque de ne pas être restauré)","強制終了（ネットワーク設定が復元されない場合あり）",
        "강제 종료(네트워크 설정이 복원되지 않을 수 있음)","Forçar encerramento (a rede pode não ser restaurada)",
        "Выйти принудительно (сеть может не восстановиться)"]},
    Message{id:"quit_stall_fallback",text:[
        "选择「是」强制退出（网络设置可能没恢复），选择「否」继续等待。",
        "Choose Yes to force quit (network settings may not be restored), or No to keep waiting.",
        "Wählen Sie „Ja“, um das Beenden zu erzwingen (Netzwerk evtl. nicht wiederhergestellt), oder „Nein“, um weiter zu warten.",
        "Elige «Sí» para forzar el cierre (puede que la red no se restaure) o «No» para seguir esperando.",
        "Choisissez «\u{a0}Oui\u{a0}» pour forcer la fermeture (le réseau risque de ne pas être restauré) ou «\u{a0}Non\u{a0}» pour continuer à attendre.",
        "「はい」を選ぶと強制終了します（ネットワーク設定が復元されない場合があります）。「いいえ」を選ぶと待機を続けます。",
        "“예”를 선택하면 강제 종료하고(네트워크 설정이 복원되지 않을 수 있음), “아니요”를 선택하면 계속 기다립니다.",
        "Escolha “Sim” para forçar o encerramento (a rede pode não ser restaurada) ou “Não” para continuar aguardando.",
        "Нажмите «Да», чтобы выйти принудительно (сеть может не восстановиться), или «Нет», чтобы продолжить ожидание."]},
    Message{id:"sidecar_prehello",text:[
        "Tesseract 的后台服务在启动完成前退出了，Tesseract 无法继续运行。",
        "Tesseract's background service exited before it finished starting. Tesseract cannot continue.",
        "Der Hintergrunddienst von Tesseract wurde beendet, bevor er vollständig gestartet war. Tesseract kann nicht fortfahren.",
        "El servicio en segundo plano de Tesseract se ha cerrado antes de terminar de iniciarse. Tesseract no puede continuar.",
        "Le service en arrière-plan de Tesseract s’est arrêté avant la fin du démarrage. Tesseract ne peut pas continuer.",
        "Tesseract のバックグラウンドサービスが起動完了前に終了しました。Tesseract は続行できません。",
        "Tesseract의 백그라운드 서비스가 시작을 마치기 전에 종료되었습니다. Tesseract를 계속 실행할 수 없습니다.",
        "O serviço em segundo plano do Tesseract foi encerrado antes de concluir a inicialização. O Tesseract não pode continuar.",
        "Фоновая служба Tesseract завершилась до окончания запуска. Tesseract не может продолжить работу."]},
    Message{id:"sidecar_prehello_handshake",text:[
        "Tesseract 的后台服务没有按时完成启动握手（或握手内容不符），Tesseract 已停止它，无法继续运行。",
        "Tesseract's background service did not complete its startup handshake in time (or the handshake did not match). Tesseract stopped it and cannot continue.",
        "Der Hintergrunddienst von Tesseract hat den Start-Handshake nicht rechtzeitig abgeschlossen (oder der Handshake stimmte nicht überein). Tesseract hat ihn beendet und kann nicht fortfahren.",
        "El servicio en segundo plano de Tesseract no ha completado a tiempo el protocolo de enlace de inicio (o su contenido no coincidía). Tesseract lo ha detenido y no puede continuar.",
        "Le service en arrière-plan de Tesseract n’a pas terminé à temps la négociation de démarrage (ou celle-ci ne correspondait pas). Tesseract l’a arrêté et ne peut pas continuer.",
        "Tesseract のバックグラウンドサービスが起動時のハンドシェイクを時間内に完了しませんでした（または内容が一致しませんでした）。Tesseract はサービスを停止したため、続行できません。",
        "Tesseract의 백그라운드 서비스가 시작 핸드셰이크를 제시간에 완료하지 못했습니다(또는 내용이 일치하지 않았습니다). Tesseract가 서비스를 중지했으며 계속 실행할 수 없습니다.",
        "O serviço em segundo plano do Tesseract não concluiu a tempo o handshake de inicialização (ou ele não correspondia). O Tesseract o interrompeu e não pode continuar.",
        "Фоновая служба Tesseract не завершила вовремя стартовое согласование (или его содержимое не совпало). Tesseract остановил службу и не может продолжить работу."]},
    Message{id:"sidecar_prehello_tail",text:[
        "后台服务日志的最后几行（已隐去敏感信息）：","Last lines of the background service log (sensitive values hidden):",
        "Letzte Zeilen des Hintergrunddienst-Protokolls (vertrauliche Werte ausgeblendet):","Últimas líneas del registro del servicio en segundo plano (valores confidenciales ocultos):",
        "Dernières lignes du journal du service en arrière-plan (valeurs sensibles masquées)\u{a0}:","バックグラウンドサービスのログの最後の数行（機密情報は伏せています）：",
        "백그라운드 서비스 로그의 마지막 몇 줄(민감한 값은 가림):","Últimas linhas dos registros do serviço em segundo plano (valores sensíveis ocultos):",
        "Последние строки журнала фоновой службы (конфиденциальные значения скрыты):"]},
    Message{id:"sidecar_prehello_nolog",text:[
        "没有找到后台服务日志 sidecar-stderr.log。","The background service log sidecar-stderr.log was not found.",
        "Das Hintergrunddienst-Protokoll sidecar-stderr.log wurde nicht gefunden.","No se ha encontrado el registro del servicio en segundo plano sidecar-stderr.log.",
        "Le journal du service en arrière-plan sidecar-stderr.log est introuvable.","バックグラウンドサービスのログ sidecar-stderr.log が見つかりません。",
        "백그라운드 서비스 로그 sidecar-stderr.log를 찾을 수 없습니다.","O arquivo de registros do serviço em segundo plano sidecar-stderr.log não foi encontrado.",
        "Журнал фоновой службы sidecar-stderr.log не найден."]},
    Message{id:"open_logs",text:["打开日志目录","Open log folder","Protokollordner öffnen","Abrir carpeta de registros","Ouvrir le dossier des journaux","ログフォルダーを開く","로그 폴더 열기","Abrir pasta de registros","Открыть папку журналов"]},
    Message{id:"open_logs_question",text:["是否打开日志目录？","Open the log folder?","Protokollordner öffnen?","¿Abrir la carpeta de registros?","Ouvrir le dossier des journaux\u{a0}?","ログフォルダーを開きますか？","로그 폴더를 여시겠습니까?","Abrir a pasta de registros?","Открыть папку журналов?"]},
    Message{id:"close",text:["关闭","Close","Schließen","Cerrar","Fermer","閉じる","닫기","Fechar","Закрыть"]},
    Message{id:"session_end_reason",text:["正在恢复网络设置","Restoring network settings","Netzwerkeinstellungen werden wiederhergestellt","Restaurando la configuración de red","Restauration des paramètres réseau","ネットワーク設定を復元しています","네트워크 설정을 복원하는 중","Restaurando as configurações de rede","Восстановление сетевых настроек"]},
    // T-P3-06 host commands: save dialog titles and file types, the external-link question.
    Message{id:"save_bundle_title",text:["保存诊断包","Save diagnostic bundle","Diagnosepaket speichern","Guardar paquete de diagnóstico","Enregistrer le paquet de diagnostic","診断パッケージを保存","진단 패키지 저장","Salvar pacote de diagnóstico","Сохранить диагностический пакет"]},
    Message{id:"save_logs_title",text:["导出日志","Export logs","Protokolle exportieren","Exportar registros","Exporter les journaux","ログをエクスポート","로그 내보내기","Exportar registros","Экспорт журналов"]},
    Message{id:"filter_zip",text:["ZIP 压缩包","ZIP archive","ZIP-Archiv","Archivo ZIP","Archive ZIP","ZIP アーカイブ","ZIP 압축 파일","Arquivo ZIP","ZIP-архив"]},
    Message{id:"filter_json",text:["JSON 文件","JSON file","JSON-Datei","Archivo JSON","Fichier JSON","JSON ファイル","JSON 파일","Arquivo JSON","Файл JSON"]},
    Message{id:"filter_text",text:["文本文件","Text file","Textdatei","Archivo de texto","Fichier texte","テキストファイル","텍스트 파일","Arquivo de texto","Текстовый файл"]},
    Message{id:"filter_csv",text:["CSV 文件","CSV file","CSV-Datei","Archivo CSV","Fichier CSV","CSV ファイル","CSV 파일","Arquivo CSV","Файл CSV"]},
    Message{id:"open_url_confirm",text:[
        "将在浏览器中打开 {host}。",
        "This will open {host} in your browser.",
        "{host} wird im Browser geöffnet.",
        "Se abrirá {host} en el navegador.",
        "{host} va s’ouvrir dans le navigateur.",
        "ブラウザーで {host} を開きます。",
        "브라우저에서 {host}을(를) 엽니다.",
        "{host} será aberto no navegador.",
        "{host} откроется в браузере."]},
    Message{id:"open_url_open",text:["打开","Open","Öffnen","Abrir","Ouvrir","開く","열기","Abrir","Открыть"]},
    Message{id:"cancel",text:["取消","Cancel","Abbrechen","Cancelar","Annuler","キャンセル","취소","Cancelar","Отмена"]},
    // T-P3-07 WebView2: no runtime installed (pre-hello, §7.10), browser process lost for good.
    Message{id:"webview2_missing",text:[
        "Tesseract 需要 Microsoft Edge WebView2 运行时才能显示界面，本机没有找到它。请安装 WebView2 运行时后重新启动 Tesseract。",
        "Tesseract needs the Microsoft Edge WebView2 Runtime to show its window, and it was not found on this computer. Install the WebView2 Runtime, then start Tesseract again.",
        "Tesseract benötigt zum Anzeigen seines Fensters die Microsoft Edge WebView2-Runtime, die auf diesem Computer jedoch nicht gefunden wurde. Installieren Sie die WebView2-Runtime und starten Sie Tesseract dann erneut.",
        "Tesseract necesita Microsoft Edge WebView2 Runtime para mostrar su ventana, pero no se ha encontrado en este equipo. Instala WebView2 Runtime y vuelve a iniciar Tesseract.",
        "Tesseract a besoin du runtime Microsoft Edge WebView2 pour afficher sa fenêtre, mais il est introuvable sur cet ordinateur. Installez le runtime WebView2, puis relancez Tesseract.",
        "Tesseract の画面を表示するには Microsoft Edge WebView2 ランタイムが必要ですが、このコンピューターに見つかりません。WebView2 ランタイムをインストールしてから、Tesseract をもう一度起動してください。",
        "Tesseract 창을 표시하려면 Microsoft Edge WebView2 런타임이 필요하지만 이 컴퓨터에서 찾을 수 없습니다. WebView2 런타임을 설치한 후 Tesseract를 다시 시작하세요.",
        "O Tesseract precisa do Microsoft Edge WebView2 Runtime para exibir a janela, mas ele não foi encontrado neste computador. Instale o WebView2 Runtime e inicie o Tesseract novamente.",
        "Для отображения окна Tesseract нужна среда выполнения Microsoft Edge WebView2, но на этом компьютере она не найдена. Установите среду выполнения WebView2 и снова запустите Tesseract."]},
    Message{id:"webview2_missing_link",text:[
        "离线安装包的下载说明：\n{url}",
        "Offline installer download:\n{url}",
        "Download des Offline-Installationsprogramms:\n{url}",
        "Descarga del instalador sin conexión:\n{url}",
        "Téléchargement du programme d’installation hors connexion\u{a0}:\n{url}",
        "オフラインインストーラーのダウンロード：\n{url}",
        "오프라인 설치 프로그램 다운로드:\n{url}",
        "Download do instalador offline:\n{url}",
        "Загрузка автономного установщика:\n{url}"]},
    Message{id:"webview2_open_download",text:["打开下载页","Open download page","Downloadseite öffnen","Abrir página de descarga","Ouvrir la page de téléchargement","ダウンロードページを開く","다운로드 페이지 열기","Abrir página de download","Открыть страницу загрузки"]},
    Message{id:"webview_lost",text:[
        "Tesseract 的界面组件（WebView2）意外退出，多次重建都没有成功。Tesseract 会先恢复网络设置，然后退出。",
        "Tesseract's window component (WebView2) exited unexpectedly and could not be recreated. Tesseract will restore network settings and then quit.",
        "Die Fensterkomponente von Tesseract (WebView2) wurde unerwartet beendet und konnte auch nach mehreren Versuchen nicht neu erstellt werden. Tesseract stellt zuerst die Netzwerkeinstellungen wieder her und wird dann beendet.",
        "El componente de ventana de Tesseract (WebView2) se ha cerrado inesperadamente y no se ha podido volver a crear tras varios intentos. Tesseract restaurará primero la configuración de red y después se cerrará.",
        "Le composant de fenêtre de Tesseract (WebView2) s’est arrêté de façon inattendue et n’a pas pu être recréé malgré plusieurs tentatives. Tesseract va d’abord restaurer les paramètres réseau, puis se fermer.",
        "Tesseract の画面コンポーネント（WebView2）が予期せず終了し、何度か再作成を試みましたが成功しませんでした。Tesseract はネットワーク設定を復元してから終了します。",
        "Tesseract의 창 구성 요소(WebView2)가 예기치 않게 종료되었고 여러 번 다시 만들려 했지만 실패했습니다. Tesseract는 네트워크 설정을 먼저 복원한 후 종료합니다.",
        "O componente de janela do Tesseract (WebView2) foi encerrado inesperadamente e não pôde ser recriado após várias tentativas. O Tesseract vai restaurar primeiro as configurações de rede e depois será encerrado.",
        "Компонент окна Tesseract (WebView2) неожиданно завершился, и его не удалось создать заново после нескольких попыток. Tesseract сначала восстановит сетевые настройки, а затем завершит работу."]},
];
pub fn language(lang:u16)->&'static str {match lang&0x3ff {
    0x04=>"zh_CN",0x07=>"de_DE",0x0a=>"es_ES",0x0c=>"fr_FR",0x11=>"ja_JP",
    0x12=>"ko_KR",0x16=>"pt_BR",0x19=>"ru_RU",_=>"en_US",
}}
pub fn text(id:&str,locale:&str)->&'static str {
    let index=LOCALES.iter().position(|l|*l==locale).unwrap_or(1);
    MESSAGES.iter().find(|m|m.id==id).unwrap_or(&MESSAGES[1]).text[index]
}
/// One of the nine supported locales, or None (callers keep their current locale).
pub fn supported(locale:&str)->Option<&'static str> {LOCALES.iter().copied().find(|l|*l==locale)}
pub fn format(id:&str,locale:&str,value:&str)->String {text(id,locale).replace("{tier}",value)}
/// Replaces each `{name}` placeholder (P3-04 messages carry at most one).
pub fn fill(id:&str,locale:&str,values:&[(&str,&str)])->String {
    let mut out=text(id,locale).to_owned();
    for (name,value) in values {out=out.replace(&format!("{{{name}}}"),value);}
    out
}
#[link(name="kernel32")] extern "system" {fn GetUserDefaultUILanguage()->u16;}
#[link(name="user32")] extern "system" {fn MessageBoxW(hwnd:usize,text:*const u16,caption:*const u16,style:u32)->i32;}
/// §7.10 pre-hello language: the user's Windows UI language.
pub fn system_locale()->&'static str {language(unsafe{GetUserDefaultUILanguage()})}
pub fn show(id:&str) {
    let locale=system_locale();
    let wide=|s:&str|s.encode_utf16().chain(Some(0)).collect::<Vec<_>>();
    unsafe {MessageBoxW(0,wide(text(id,locale)).as_ptr(),wide(text("title",locale)).as_ptr(),0x10);}
}
#[cfg(test)] mod tests {
    use super::*;
    /// T-P5-01: all nine locales are authoritative (no review flag left); each text is non-empty.
    #[test] fn all_messages_have_nine_authoritative_locales() {
        assert_eq!(MESSAGES.len(),37);
        for message in MESSAGES {assert_eq!(message.text.len(),9);assert!(message.text.iter().all(|t|!t.trim().is_empty()),"{}",message.id);}
        let mut ids:Vec<_>=MESSAGES.iter().map(|m|m.id).collect(); ids.sort_unstable(); ids.dedup();
        assert_eq!(ids.len(),MESSAGES.len(),"duplicate message id");
        for (id,locale) in [(0x804,"zh_CN"),(0x409,"en_US"),(0x407,"de_DE"),(0x40a,"es_ES"),(0x40c,"fr_FR"),(0x411,"ja_JP"),(0x412,"ko_KR"),(0x416,"pt_BR"),(0x419,"ru_RU"),(0xffff,"en_US")] {assert_eq!(language(id),locale);}
        assert_eq!(text("instance_unknown","unknown"),text("instance_unknown","en_US"));
    }
    /// `{name}` placeholders of a text, sorted (a multiset: count matters).
    fn placeholders(s:&str)->Vec<&str> {
        let mut out=Vec::new(); let mut rest=s;
        while let Some(open)=rest.find('{') {
            let tail=&rest[open+1..];
            match tail.find('}') {
                Some(close) if close>0 && tail[..close].chars().all(|c|c.is_ascii_alphanumeric()||c=='_') => {out.push(&tail[..close]); rest=&tail[close+1..];},
                _ => rest=tail,
            }
        }
        out.sort_unstable(); out
    }
    /// T-P5-01 item 5: every locale carries exactly zh_CN's placeholders (`format` / `fill` replace them by name).
    #[test] fn placeholders_match_zh_cn_in_every_locale() {
        assert_eq!(placeholders("a {dir}\\x {dir} {count}"),["count","dir","dir"]);
        assert!(placeholders("{} {a-b} {").is_empty());
        for message in MESSAGES {
            let zh=placeholders(message.text[0]);
            for (locale,t) in LOCALES.iter().zip(message.text) { assert_eq!(placeholders(t),zh,"{} {locale}",message.id); }
        }
    }
    /// T-P5-01 item 5: es_ES addresses the user with tú (译文风格.md), never usted imperatives.
    #[test] fn es_es_uses_tu_not_usted() {
        let es=LOCALES.iter().position(|l|*l=="es_ES").unwrap();
        for message in MESSAGES {
            let t=message.text[es].to_lowercase();
            for usted in ["usted","inténtelo","intente","compruebe","verifique","reinicie","inicie","elija","ejecute","instale","instálelo","vuelva",
                "fuerce","si fuerza ",", puede ","seleccione","cierre la","abra ","espere","pulse","haga clic","asegúrese"] {
                assert!(!t.contains(usted),"{}: usted form {usted:?} in {t:?}",message.id);
            }
        }
        // And the tú forms are really there (a usted rewrite with another verb would drop them).
        assert!(text("instance_unknown","es_ES").contains("Comprueba") && text("instance_unknown","es_ES").contains("vuelve a intentarlo"));
        for id in ["quitting_timeout","runtime_timeout"] { assert!(text(id,"es_ES").contains("Vuelve a intentarlo"),"{id}"); }
        assert!(text("quit_stall_scripts","es_ES").starts_with("Si fuerzas el cierre, puedes") && text("quit_stall_scripts","es_ES").contains("ejecuta unkill.bat"));
        assert!(text("quit_stall_fallback","es_ES").starts_with("Elige"));
        assert!(text("webview2_missing","es_ES").contains("Instala WebView2") && text("webview2_missing","es_ES").contains("vuelve a iniciar"));
    }
    /// 译文风格.md punctuation: fr puts a no-break space (U+00A0 or U+202F) right before every
    /// `: ; ! ?` (fr texts carry no URL or path colon; those come in through `{url}` / `{dir}`),
    /// ja uses full-width `：？`.
    #[test] fn locale_punctuation_follows_the_style_guide() {
        let at=|l:&str|LOCALES.iter().position(|x|*x==l).unwrap();
        let fr_ok=|s:&str|{let mut prev=None; s.chars().all(|c|{let ok=!matches!(c,':'|';'|'!'|'?')||matches!(prev,Some('\u{a0}'|'\u{202f}')); prev=Some(c); ok})};
        assert!(fr_ok("Niveau actuel\u{a0}: {tier}") && !fr_ok("Niveau actuel: {tier}") && !fr_ok("Niveau actuel : {tier}") && !fr_ok(":"));
        for message in MESSAGES {
            let fr=message.text[at("fr_FR")];
            assert!(fr_ok(fr),"{}: fr needs U+00A0 / U+202F before : ; ! ? in {fr:?}",message.id);
            let ja=message.text[at("ja_JP")];
            assert!(!ja.contains('?') && !ja.contains(": ") && !ja.ends_with(':'),"{}: ja half-width punctuation",message.id);
        }
    }
    /// DESIGN §6.11 (F-03): the codename is never translated and stands alone; the tray line reads
    /// `<label>: <Codename>` in every locale, like the page.
    #[test] fn tray_tier_line_names_the_tier_alone() {
        assert_eq!(format("tray_tier_line","de_DE","Gateway"),"Aktuelle Stufe: Gateway");
        assert!(MESSAGES.iter().all(|m|!m.id.starts_with("tier_role_")));
        // tray_offline is only the value after "label:", so it is lowercase there.
        for locale in ["en_US","de_DE","es_ES","fr_FR","pt_BR","ru_RU"] {
            assert!(text("tray_offline",locale).chars().next().is_some_and(char::is_lowercase),"{locale}");
        }
        assert_eq!(format("tray_tier_line","fr_FR",text("tray_offline","fr_FR")),"Niveau actuel\u{a0}: hors ligne");
        // T-P5-09 ruling 1: en follows suit.
        assert_eq!(format("tray_tier_line","en_US",text("tray_offline","en_US")),"Current tier: offline");
        // The page catalogs carry no role words either; read them, so a role word added back on
        // the page side fails here too.
        for locale in LOCALES {
            let path=std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join(format!("../../ui/src/i18n/locales/{locale}/tier.json"));
            let bytes=std::fs::read(&path).unwrap_or_else(|e|panic!("{}: {e}",path.display()));
            let page:serde_json::Value=serde_json::from_slice(&bytes).unwrap_or_else(|e|panic!("{}: {e}",path.display()));
            for tier in ["vanguard","sentinel","bastion","gateway"] {
                assert!(page.get(format!("tier.{tier}.role")).is_none(),"{}: tier.{tier}.role",path.display());
            }
        }
        assert_eq!(format("tray_tier_line","en_US","Vanguard"),"Current tier: Vanguard");
    }
    /// T-P5-05 ruling 2: the sidecar is the "background service" in every locale, worded as the
    /// page's shell.sidecar.* / shell.host_reason.* entries; the word "process" is gone. The log's
    /// file name stays `sidecar-stderr.log`.
    #[test] fn sidecar_is_the_background_service_as_on_the_page() {
        let service=["后台服务","background service","hintergrunddienst","servicio en segundo plano","service en arrière-plan","バックグラウンドサービス","백그라운드 서비스","serviço em segundo plano","фонов"];
        for id in ["runtime_timeout","sidecar_prehello","sidecar_prehello_handshake","sidecar_prehello_tail","sidecar_prehello_nolog"] {
            for (locale,term) in LOCALES.iter().zip(service) {
                let t=text(id,locale).to_lowercase();
                assert!(t.contains(term),"{id} {locale}: {t:?} lacks {term:?}");
                if *locale=="ru_RU" { assert!(["фоновая служба","фоновой службы","фоновую службу"].iter().any(|f|t.contains(f)),"{id} ru: {t:?}"); }
            }
        }
        // Word stems, not phrases: a lone "Prozess" / "el proceso" / "процесса" is caught too.
        for message in MESSAGES { for (locale,t) in LOCALES.iter().zip(message.text) {
            let t=t.to_lowercase();
            for process in ["进程","process","prozess","proceso","processus","プロセス","프로세스","processo","процесс"] {
                assert!(!t.contains(process),"{} {locale}: {process:?}",message.id);
            }
        }}
        for locale in LOCALES { assert!(text("sidecar_prehello_nolog",locale).contains("sidecar-stderr.log"),"{locale}"); }
        // T-P5-05 §8 item 3, confirmed by T-P5-09 ruling 3: the stopped object is the service too.
        assert!(text("sidecar_prehello_handshake","ja_JP").contains("サービスを停止"));
        assert!(text("sidecar_prehello_handshake","ko_KR").contains("서비스를 중지"));
        assert!(text("sidecar_prehello_handshake","ru_RU").contains("остановил службу"));
    }
    /// T-P5-09 ruling 2: pt_BR says "registros" for logs, as the page does (shell.nav.logs
    /// "Registros", shell.action.open_logs "Abrir registros do serviço em segundo plano"); the
    /// file name `sidecar-stderr.log` stays.
    #[test] fn pt_br_logs_are_registros_as_on_the_page() {
        for id in ["open_logs","open_logs_question","sidecar_prehello_tail","sidecar_prehello_nolog","save_logs_title"] {
            let t=text(id,"pt_BR");
            assert!(t.contains("registros"),"{id}: {t:?}");
        }
        assert!(text("sidecar_prehello_tail","pt_BR").contains("registros do serviço em segundo plano"));
        assert!(text("sidecar_prehello_nolog","pt_BR").contains("sidecar-stderr.log"));
        // The page's own "open log folder" action, read from its catalog so neither side drifts alone
        // (the logs page's menu item; the settings page's data-folder row went in 配置整理 P1, 11-04).
        let path=std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../ui/src/i18n/locales/pt_BR/logs.json");
        let bytes=std::fs::read(&path).unwrap_or_else(|e|panic!("{}: {e}",path.display()));
        let page:serde_json::Value=serde_json::from_slice(&bytes).unwrap_or_else(|e|panic!("{}: {e}",path.display()));
        let open_logs=page["logs.more.folder"].as_str().unwrap_or_else(||panic!("{}: logs.more.folder missing",path.display()));
        assert_eq!(text("open_logs","pt_BR"),open_logs,"open_logs pt_BR vs {}",path.display());
        for message in MESSAGES {
            let t=message.text[LOCALES.iter().position(|l|*l=="pt_BR").unwrap()].replace("sidecar-stderr.log","").to_lowercase();
            assert!(!t.split(|c:char|!c.is_alphanumeric()).any(|w|w=="log"||w=="logs"),"{} pt_BR: {t:?}",message.id);
        }
    }
    #[test] fn tray_texts_cover_nine_locales_with_authoritative_zh_en() {
        let ids=["tray_show","tray_quit","tray_tier_line","tray_offline","tray_core_running","tray_core_stopped"];
        for id in ids {
            let message=MESSAGES.iter().find(|m|m.id==id).unwrap_or_else(||panic!("missing {id}"));
            assert!(message.text.iter().all(|t|!t.trim().is_empty()),"{id}");
        }
        for locale in LOCALES {
            assert!(text("tray_tier_line",locale).contains("{tier}"));
            assert_ne!(text("tray_show",locale),text("tray_quit",locale));
            assert!(text("tray_show",locale).contains("Tesseract"));
            assert_eq!(supported(locale),Some(locale));
        }
        assert_eq!((text("tray_show","zh_CN"),text("tray_quit","zh_CN")),("显示 Tesseract","退出"));
        assert_eq!((text("tray_show","en_US"),text("tray_quit","en_US")),("Show Tesseract","Quit"));
        assert_eq!(format("tray_tier_line","zh_CN","Vanguard"),"当前层级：Vanguard");
        assert_eq!(text("tray_core_running","zh_CN"),"代理核心 运行中");
        assert_eq!(text("tray_core_stopped","zh_CN"),"代理核心 已停止");
        assert_eq!(supported("zh-CN"),None); assert_eq!(supported(""),None);
    }
    #[test] fn quit_and_crash_texts_cover_nine_locales_with_placeholders() {
        let ids=["quit_unsaved","quit_stall","quit_stall_scripts","quit_stall_wait","quit_stall_force","quit_stall_fallback",
            "sidecar_prehello","sidecar_prehello_handshake","sidecar_prehello_tail","sidecar_prehello_nolog","open_logs","open_logs_question","close","session_end_reason"];
        for id in ids {
            let message=MESSAGES.iter().find(|m|m.id==id).unwrap_or_else(||panic!("missing {id}"));
            assert!(message.text.iter().all(|t|!t.trim().is_empty()),"{id}");
        }
        for locale in LOCALES {
            assert!(text("quit_unsaved",locale).contains("{count}"),"{locale}");
            let scripts=fill("quit_stall_scripts",locale,&[("dir","D:\\data")]);
            assert!(scripts.contains("D:\\data\\unkill.bat") && scripts.contains("D:\\data\\restore_proxy.bat"),"{locale}");
            assert_ne!(text("quit_stall_wait",locale),text("quit_stall_force",locale));
            // ShutdownBlockReasonCreate shows a short reason; keep it one line.
            assert!(!text("session_end_reason",locale).contains('\n'));
        }
        assert_eq!(fill("quit_unsaved","zh_CN",&[("count","3")]),"有 3 项更改没保存，仍然退出？");
        assert_eq!(text("quit_stall_force","zh_CN"),"强制退出（网络设置可能没恢复）");
        assert_eq!(text("quit_stall_wait","zh_CN"),"继续等待");
        assert_eq!(text("session_end_reason","zh_CN"),"正在恢复网络设置");
        assert_ne!(text("sidecar_prehello","zh_CN"),text("sidecar_prehello_handshake","zh_CN"),"a handshake failure is not \"it exited\"");
    }
    #[test] fn host_texts_cover_nine_locales_and_show_only_the_host() {
        for id in ["save_bundle_title","save_logs_title","filter_zip","filter_json","filter_text","filter_csv","open_url_confirm","open_url_open","cancel"] {
            let message=MESSAGES.iter().find(|m|m.id==id).unwrap_or_else(||panic!("missing {id}"));
            assert!(message.text.iter().all(|t|!t.trim().is_empty()),"{id}");
        }
        for locale in LOCALES {
            assert_eq!(text("open_url_confirm",locale).matches("{host}").count(),1,"{locale}");
            assert_ne!(text("open_url_open",locale),text("cancel",locale));
        }
        assert_eq!(fill("open_url_confirm","zh_CN",&[("host","github.com")]),"将在浏览器中打开 github.com。");
        assert_eq!(text("save_bundle_title","zh_CN"),"保存诊断包");
    }
    #[test] fn webview_texts_cover_nine_locales() {
        for id in ["webview2_missing","webview2_missing_link","webview2_open_download","webview_lost"] {
            let message=MESSAGES.iter().find(|m|m.id==id).unwrap_or_else(||panic!("missing {id}"));
            assert!(message.text.iter().all(|t|!t.trim().is_empty()),"{id}");
        }
        for locale in LOCALES {
            assert_eq!(text("webview2_missing_link",locale).matches("{url}").count(),1,"{locale}");
            assert!(text("webview2_missing",locale).contains("WebView2") && text("webview_lost",locale).contains("WebView2"),"{locale}");
            assert_ne!(text("webview2_open_download",locale),text("tray_quit",locale));
        }
        assert_eq!(fill("webview2_missing_link","zh_CN",&[("url","https://github.com/x")]),"离线安装包的下载说明：\nhttps://github.com/x");
    }
    /// T-P4-C3 item 9 (criticism A3): the first-run LANGID mapping shared with the sidecar
    /// (T-P4-C1 item 6 writes `bridge_schema/fixtures/lang_map_vectors.json`, its Python test
    /// asserts the same table). U3: zh-TW / zh-HK map to zh_CN, as `language` already does.
    /// C1 delivered the file; were it missing, this test is skipped (printed `SKIPPED`), and with
    /// `TESSERACT_REQUIRE_LANG_VECTORS=1` (integration, CI) a missing file fails it instead.
    #[test] fn first_run_language_matches_the_shared_vectors() {
        let path=std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../bridge_schema/fixtures/lang_map_vectors.json");
        let Ok(bytes)=std::fs::read(&path) else {
            let required=std::env::var("TESSERACT_REQUIRE_LANG_VECTORS").is_ok_and(|v|v.trim()=="1");
            assert!(!required,"TESSERACT_REQUIRE_LANG_VECTORS=1 but {} is missing (T-P4-C1 item 6)",path.display());
            eprintln!("SKIPPED first_run_language_matches_the_shared_vectors: {} not delivered yet (T-P4-C1 item 6)",path.display());
            return;
        };
        let vectors=parse_lang_vectors(&serde_json::from_slice(&bytes).expect("lang_map_vectors.json is JSON"))
            .unwrap_or_else(|e|panic!("{}: {e}",path.display()));
        assert!(!vectors.is_empty(),"no LANGID vector in {}",path.display());
        for (langid,expected) in &vectors { assert_eq!(language(*langid),expected.as_str(),"LANGID {langid:#06x}"); }
        // The table covers the nine languages and at least one unsupported one (-> en_US).
        for locale in LOCALES { assert!(vectors.iter().any(|(_,l)|l==locale),"no vector for {locale}"); }
        eprintln!("CHECKED first_run_language_matches_the_shared_vectors: {} vectors",vectors.len());
    }
    /// The one shape C1 delivered (T-P4-08b, decision wave 5 C3-5: the other shapes and their
    /// assertions are gone): `{"vectors": [{"langid": "0x0804", "expected": "zh_CN", "lang"?: "zh-CN"}]}`.
    /// `langid` is a `0x` hex string, `expected` one of the nine locales, `lang` the input tag (an
    /// optional note, never the result); top-level keys other than `vectors` must start with `_`
    /// (comments). Anything else fails the whole file, naming the entry.
    fn parse_lang_vectors(value:&serde_json::Value)->Result<Vec<(u16,String)>,String> {
        use serde_json::Value;
        let Value::Object(top)=value else { return Err(format!("not an object: {value}")); };
        if let Some(k)=top.keys().find(|k|*k!="vectors" && !k.starts_with('_')) { return Err(format!("unexpected top-level key {k}")); }
        let Some(Value::Array(list))=top.get("vectors") else { return Err("vectors is missing or not an array".to_owned()); };
        list.iter().enumerate().map(|(i,v)|{
            let Value::Object(entry)=v else { return Err(format!("entry {i} is not an object: {v}")); };
            if let Some(k)=entry.keys().find(|k|!matches!(k.as_str(),"langid"|"expected"|"lang")) { return Err(format!("entry {i}: unexpected key {k}")); }
            let langid=entry.get("langid").and_then(Value::as_str).and_then(|s|s.strip_prefix("0x")).and_then(|hex|u16::from_str_radix(hex,16).ok())
                .ok_or_else(||format!("entry {i}: langid must be a 0x hex string: {v}"))?;
            let expected=entry.get("expected").and_then(Value::as_str).and_then(supported)
                .ok_or_else(||format!("entry {i}: expected must be one of the nine locales: {v}"))?;
            if entry.get("lang").is_some_and(|l|!l.is_string()) { return Err(format!("entry {i}: lang must be a string: {v}")); }
            Ok((langid,expected.to_owned()))
        }).collect()
    }
    #[test] fn lang_vector_shapes_parse_and_the_d4_mapping_holds() {
        use serde_json::json;
        assert_eq!(parse_lang_vectors(&json!({"_comment":"x","vectors":[{"langid":"0x0804","lang":"zh-CN","expected":"zh_CN"},{"langid":"0x0404","expected":"zh_CN"}]})),
            Ok(vec![(0x0804u16,"zh_CN".to_owned()),(0x0404,"zh_CN".to_owned())]));
        // the input tag is a note, never the result
        assert_eq!(parse_lang_vectors(&json!({"vectors":[{"langid":"0x0404","lang":"zh-TW","expected":"zh_CN"}]})),Ok(vec![(0x0404,"zh_CN".to_owned())]));
        // T-P4-08b: one shape only; nothing is dropped silently
        for bad in [json!([{"langid":"0x0804","expected":"zh_CN"}]), json!({"0x0804":"zh_CN"}), json!({"cases":[{"langid":"0x0804","expected":"zh_CN"}]}),
            json!({"vectors":{"0x0804":"zh_CN"}}), json!({"vectors":[{"langid":"0x0804","expected":"zh_CN"}],"extra":1}),
            json!({"vectors":[{"langid":2052,"expected":"zh_CN"}]}), json!({"vectors":[{"langid":"2052","expected":"zh_CN"}]}),
            json!({"vectors":[{"lang_id":"0x0804","expected":"zh_CN"}]}), json!({"vectors":[{"langid":"0x0804","language":"zh_CN"}]}),
            json!({"vectors":[{"langid":"0x0404","lang":"zh-TW"}]}), json!({"vectors":[{"langid":"0x0404","expected":"zh-TW"}]}),
            json!({"vectors":[{"langid":"0x0804","expected":"zh_CN","lang":1}]}), json!({"vectors":["0x0804"]})] {
            assert!(parse_lang_vectors(&bad).is_err(),"{bad}");
        }
        // U3 (决定_P4_用户答复.md): zh-TW / zh-HK / zh-SG / zh-MO -> zh_CN; regional variants by primary language.
        for (langid,locale) in [(0x0404,"zh_CN"),(0x0c04,"zh_CN"),(0x1004,"zh_CN"),(0x1404,"zh_CN"),(0x0816,"pt_BR"),(0x080a,"es_ES"),
            (0x0c0c,"fr_FR"),(0x0807,"de_DE"),(0x0410,"en_US"),(0x0809,"en_US")] { assert_eq!(language(langid),locale,"{langid:#06x}"); }
    }
}
