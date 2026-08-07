#include <QAction>
#include <QApplication>
#include <QColor>
#include <QDBusConnection>
#include <QDBusError>
#include <QDBusInterface>
#include <QDBusObjectPath>
#include <QDBusPendingCall>
#include <QDBusPendingCallWatcher>
#include <QDBusPendingReply>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QFileSystemWatcher>
#include <QIcon>
#include <QMenu>
#include <QMetaObject>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QObject>
#include <QPainter>
#include <QPen>
#include <QPixmap>
#include <QStringList>
#include <QSystemTrayIcon>
#include <QTimer>
#include <QUrl>
#include <QVariant>
#include <QVariantMap>

#include <PulseAudioQt/Context>
#include <PulseAudioQt/Server>
#include <PulseAudioQt/Source>

#include "tray_state.h"

#include <memory>

namespace {

constexpr const char* kControllerUnit = "okay-hermes-realtime-controller.service";
constexpr const char* kWakewordUnit = "okay-hermes-realtime-wakeword.service";
constexpr const char* kStateRootRelativePath = ".local/state/okay-hermes-realtime";
constexpr const char* kCaptureHealthFile = "capture-health";
constexpr const char* kControllerHealthFile = "controller-health";
constexpr const char* kOpenEndpoint = "http://127.0.0.1:8765/internal/open";
constexpr const char* kSystemdService = "org.freedesktop.systemd1";
constexpr const char* kSystemdManagerPath = "/org/freedesktop/systemd1";
constexpr const char* kSystemdManagerInterface = "org.freedesktop.systemd1.Manager";
constexpr const char* kSystemdUnitInterface = "org.freedesktop.systemd1.Unit";
constexpr const char* kDBusPropertiesInterface = "org.freedesktop.DBus.Properties";
constexpr int kIconSize = 64;

QIcon stateIcon(const QColor& color, int spinnerFrame = -1) {
    QPixmap pixmap(kIconSize, kIconSize);
    pixmap.fill(Qt::transparent);
    QPainter painter(&pixmap);
    painter.setRenderHint(QPainter::Antialiasing, true);
    painter.setPen(Qt::NoPen);
    painter.setBrush(QColor(17, 24, 39));
    painter.drawEllipse(2, 2, 60, 60);
    painter.setBrush(color);
    painter.drawEllipse(7, 7, 50, 50);
    painter.setBrush(Qt::white);
    painter.drawRoundedRect(24, 12, 16, 28, 8, 8);
    QPen microphonePen(Qt::white, 5, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin);
    painter.setPen(microphonePen);
    painter.setBrush(Qt::NoBrush);
    painter.drawArc(18, 22, 28, 25, 180 * 16, 180 * 16);
    painter.drawLine(32, 44, 32, 53);
    painter.drawLine(24, 53, 40, 53);
    if (spinnerFrame >= 0) {
        QPen spinnerPen(Qt::white, 6, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin);
        painter.setPen(spinnerPen);
        painter.drawArc(8, 8, 48, 48, ((spinnerFrame * 40) % 360) * 16, 95 * 16);
    }
    return QIcon(pixmap);
}

QString stateRootPath() {
    return QDir::home().filePath(kStateRootRelativePath);
}

QString captureHealthPath() {
    return QDir(stateRootPath()).filePath(kCaptureHealthFile);
}

QString controllerHealthPath() {
    return QDir(stateRootPath()).filePath(kControllerHealthFile);
}

QString readStatusFile(const QString& path) {
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) {
        return QString();
    }
    return QString::fromUtf8(file.read(128));
}

}  // namespace

using okay_hermes_realtime_tray::CaptureHealth;
using okay_hermes_realtime_tray::ControllerHealth;
using okay_hermes_realtime_tray::DaemonState;

class TrayController : public QObject {
    Q_OBJECT

public:
    explicit TrayController(QApplication& application)
        : QObject(&application),
          app(application),
          tray(std::make_unique<QSystemTrayIcon>()),
          menu(std::make_unique<QMenu>()),
          switchingTimer(new QTimer(this)),
          systemdRetryTimer(new QTimer(this)),
          captureWatcher(new QFileSystemWatcher(this)),
          controllerWatcher(new QFileSystemWatcher(this)),
          network(new QNetworkAccessManager(this)) {
        turnOnAction = menu->addAction("Turn ON");
        turnOffAction = menu->addAction("Turn OFF");
        openVoicePageAction = menu->addAction("Open Voice Page");
        menu->addSeparator();
        exitAction = menu->addAction("Exit");
        tray->setContextMenu(menu.get());
        tray->setIcon(stateIcon(QColor(234, 179, 8), 0));
        tray->setToolTip("Okay Hermes Realtime: loading…");

        QObject::connect(turnOnAction, &QAction::triggered, this, &TrayController::startServices);
        QObject::connect(turnOffAction, &QAction::triggered, this, &TrayController::stopServices);
        QObject::connect(openVoicePageAction, &QAction::triggered, this, &TrayController::openVoicePage);
        QObject::connect(exitAction, &QAction::triggered, &app, &QApplication::quit);
        QObject::connect(switchingTimer, &QTimer::timeout, this, &TrayController::advanceSpinner);
        QObject::connect(captureWatcher, &QFileSystemWatcher::directoryChanged, this, &TrayController::captureHealthChanged);
        QObject::connect(captureWatcher, &QFileSystemWatcher::fileChanged, this, &TrayController::captureHealthChanged);
        QObject::connect(controllerWatcher, &QFileSystemWatcher::directoryChanged, this, &TrayController::controllerHealthChanged);
        QObject::connect(controllerWatcher, &QFileSystemWatcher::fileChanged, this, &TrayController::controllerHealthChanged);

        systemdRetryTimer->setSingleShot(true);
        systemdRetryTimer->setInterval(3000);
        QObject::connect(systemdRetryTimer, &QTimer::timeout, this, &TrayController::setupSystemdWatchers);

        setupStateWatchers();
        setupAudioWatcher();
        setupSystemdWatchers();
        requestUnitStates();
        setSwitchingState("Loading replacement service state…");
        QTimer::singleShot(150, this, &TrayController::refreshState);
    }

    int run() {
        if (!QSystemTrayIcon::isSystemTrayAvailable()) {
            qCritical("No system tray is available in this desktop session.");
            return 1;
        }
        tray->show();
        return app.exec();
    }

private Q_SLOTS:
    void refreshState() {
        if (systemdCommandInFlight) {
            return;
        }
        const DaemonState state = daemonState();
        switch (state) {
        case DaemonState::NoMicrophone:
            switchingTimer->stop();
            turnOnAction->setEnabled(false);
            turnOffAction->setEnabled(anyControlledUnitActive());
            openVoicePageAction->setEnabled(controllerActive);
            tray->setToolTip(microphoneAvailable()
                                 ? "Okay Hermes Realtime: microphone not capturing yet"
                                 : "Okay Hermes Realtime: no microphone available");
            tray->setIcon(stateIcon(QColor(107, 114, 128)));
            return;
        case DaemonState::Error:
            switchingTimer->stop();
            turnOnAction->setEnabled(false);
            turnOffAction->setEnabled(anyControlledUnitActive());
            openVoicePageAction->setEnabled(controllerActive);
            tray->setToolTip("Okay Hermes Realtime: capture or controller error");
            tray->setIcon(stateIcon(QColor(220, 38, 38)));
            return;
        case DaemonState::Starting:
            turnOnAction->setEnabled(false);
            turnOffAction->setEnabled(false);
            openVoicePageAction->setEnabled(controllerActive);
            tray->setToolTip("Okay Hermes Realtime: starting…");
            if (!switchingTimer->isActive()) {
                switchingTimer->start(150);
            }
            tray->setIcon(stateIcon(QColor(234, 179, 8), spinnerFrame));
            return;
        case DaemonState::ConversationActive:
            switchingTimer->stop();
            turnOnAction->setEnabled(false);
            turnOffAction->setEnabled(true);
            openVoicePageAction->setEnabled(false);
            tray->setToolTip("Okay Hermes Realtime: conversation active");
            tray->setIcon(stateIcon(QColor(59, 130, 246)));
            return;
        case DaemonState::On:
            switchingTimer->stop();
            turnOnAction->setEnabled(false);
            turnOffAction->setEnabled(true);
            openVoicePageAction->setEnabled(true);
            tray->setToolTip("Okay Hermes Realtime: ON");
            tray->setIcon(stateIcon(QColor(34, 197, 94)));
            return;
        case DaemonState::Off:
            switchingTimer->stop();
            turnOnAction->setEnabled(microphoneAvailable());
            turnOffAction->setEnabled(false);
            openVoicePageAction->setEnabled(false);
            tray->setToolTip("Okay Hermes Realtime: OFF");
            tray->setIcon(stateIcon(QColor(239, 68, 68)));
            return;
        }
    }

    void systemdStateChanged() {
        bool connected = connectUnitSignals(kControllerUnit);
        connected = connectUnitSignals(kWakewordUnit) && connected;
        if (!connected) {
            scheduleSystemdWatcherRetry();
        }
        requestUnitStates();
    }

    void systemdJobRemoved(uint, const QDBusObjectPath&, const QString&, const QString&) {
        systemdStateChanged();
    }

    void systemdUnitChanged(const QString&, const QDBusObjectPath&) {
        systemdStateChanged();
    }

    void systemdPropertiesChanged(const QString&, const QVariantMap&, const QStringList&) {
        systemdStateChanged();
    }

    void captureHealthChanged() {
        setupStateWatchers();
        refreshState();
    }

    void controllerHealthChanged() {
        setupStateWatchers();
        refreshState();
    }

    void audioStateChanged() {
        refreshState();
    }

    void advanceSpinner() {
        ++spinnerFrame;
        tray->setIcon(stateIcon(QColor(234, 179, 8), spinnerFrame));
    }

private:
    QDBusInterface systemdManager() const {
        return QDBusInterface(
            kSystemdService,
            kSystemdManagerPath,
            kSystemdManagerInterface,
            QDBusConnection::sessionBus());
    }

    void startServices() {
        if (!microphoneAvailable()) {
            refreshState();
            return;
        }
        setSwitchingState("Starting replacement services…");
        runSystemdCommandsAsync("StartUnit", {kControllerUnit, kWakewordUnit});
    }

    void stopServices() {
        setSwitchingState("Stopping replacement services…");
        runSystemdCommandsAsync("StopUnit", {kWakewordUnit, kControllerUnit});
    }

    void openVoicePage() {
        if (!controllerActive || openRequestInFlight) {
            return;
        }
        openRequestInFlight = true;
        openVoicePageAction->setEnabled(false);
        QNetworkRequest request(QUrl(QString::fromLatin1(kOpenEndpoint)));
        request.setHeader(QNetworkRequest::ContentTypeHeader, QStringLiteral("application/json"));
        QNetworkReply* reply = network->post(request, QByteArray());
        QObject::connect(reply, &QNetworkReply::finished, this, [this, reply] {
            const bool ok = reply->error() == QNetworkReply::NoError;
            if (!ok) {
                tray->showMessage(
                    "Okay Hermes Realtime",
                    "Controller could not open the voice page.",
                    QSystemTrayIcon::Warning,
                    4000);
            }
            reply->deleteLater();
            openRequestInFlight = false;
            refreshState();
        });
    }

    void setSwitchingState(const QString& tooltip) {
        spinnerFrame = 0;
        turnOnAction->setEnabled(false);
        turnOffAction->setEnabled(false);
        openVoicePageAction->setEnabled(false);
        tray->setToolTip(tooltip);
        tray->setIcon(stateIcon(QColor(234, 179, 8), spinnerFrame));
        switchingTimer->start(150);
    }

    void requestUnitStates() {
        requestUnitState(kControllerUnit);
        requestUnitState(kWakewordUnit);
    }

    void requestUnitState(const QString& unit) {
        QDBusPendingCall pending = systemdManager().asyncCall("GetUnit", unit);
        auto* watcher = new QDBusPendingCallWatcher(pending, this);
        QObject::connect(watcher, &QDBusPendingCallWatcher::finished, this,
                         [this, unit](QDBusPendingCallWatcher* finished) {
            QDBusPendingReply<QDBusObjectPath> reply(*finished);
            finished->deleteLater();
            if (!reply.isValid() || reply.value().path().isEmpty()) {
                setCachedUnitActive(unit, false);
                refreshState();
                return;
            }
            requestUnitActiveState(unit, reply.value().path());
        });
    }

    void requestUnitActiveState(const QString& unit, const QString& path) {
        QDBusInterface properties(
            kSystemdService,
            path,
            kDBusPropertiesInterface,
            QDBusConnection::sessionBus());
        QDBusPendingCall pending = properties.asyncCall("Get", kSystemdUnitInterface, "ActiveState");
        auto* watcher = new QDBusPendingCallWatcher(pending, this);
        QObject::connect(watcher, &QDBusPendingCallWatcher::finished, this,
                         [this, unit](QDBusPendingCallWatcher* finished) {
            QDBusPendingReply<QVariant> reply(*finished);
            finished->deleteLater();
            setCachedUnitActive(unit, reply.isValid() && reply.value().toString() == "active");
            refreshState();
        });
    }

    void setCachedUnitActive(const QString& unit, bool active) {
        if (unit == QLatin1String(kControllerUnit)) {
            controllerActive = active;
        } else if (unit == QLatin1String(kWakewordUnit)) {
            wakewordActive = active;
        }
    }

    void runSystemdCommandsAsync(const QString& method, const QStringList& units) {
        if (systemdCommandInFlight) {
            return;
        }
        systemdCommandInFlight = true;
        runSystemdCommandAt(method, units, 0);
    }

    void runSystemdCommandAt(const QString& method, const QStringList& units, int index) {
        if (index >= units.size()) {
            systemdCommandInFlight = false;
            requestUnitStates();
            return;
        }
        QDBusPendingCall pending = systemdManager().asyncCall(
            method, units.at(index), QStringLiteral("replace"));
        auto* watcher = new QDBusPendingCallWatcher(pending, this);
        QObject::connect(watcher, &QDBusPendingCallWatcher::finished, this,
                         [this, method, units, index](QDBusPendingCallWatcher* finished) {
            QDBusPendingReply<QDBusObjectPath> reply(*finished);
            finished->deleteLater();
            if (!reply.isValid()) {
                const QDBusError error = reply.error();
                Q_UNUSED(error);
                tray->showMessage(
                    "Okay Hermes Realtime",
                    "A replacement service state change failed.",
                    QSystemTrayIcon::Warning,
                    4000);
                systemdCommandInFlight = false;
                requestUnitStates();
                return;
            }
            runSystemdCommandAt(method, units, index + 1);
        });
    }

    void scheduleSystemdWatcherRetry() {
        if (!systemdRetryTimer->isActive()) {
            systemdRetryTimer->start();
        }
    }

    void setupSystemdWatchers() {
        bool allConnected = true;
        QDBusPendingCall subscribePending = systemdManager().asyncCall("Subscribe");
        auto* subscribeWatcher = new QDBusPendingCallWatcher(subscribePending, this);
        QObject::connect(subscribeWatcher, &QDBusPendingCallWatcher::finished, this,
                         [this](QDBusPendingCallWatcher* finished) {
            QDBusPendingReply<void> reply(*finished);
            finished->deleteLater();
            if (!reply.isValid()) {
                scheduleSystemdWatcherRetry();
            }
        });

        auto bus = QDBusConnection::sessionBus();
        if (!jobRemovedSignalConnected) {
            jobRemovedSignalConnected = bus.connect(
                kSystemdService, kSystemdManagerPath, kSystemdManagerInterface, "JobRemoved",
                this, SLOT(systemdJobRemoved(uint,QDBusObjectPath,QString,QString)));
        }
        if (!unitNewSignalConnected) {
            unitNewSignalConnected = bus.connect(
                kSystemdService, kSystemdManagerPath, kSystemdManagerInterface, "UnitNew",
                this, SLOT(systemdUnitChanged(QString,QDBusObjectPath)));
        }
        if (!unitRemovedSignalConnected) {
            unitRemovedSignalConnected = bus.connect(
                kSystemdService, kSystemdManagerPath, kSystemdManagerInterface, "UnitRemoved",
                this, SLOT(systemdUnitChanged(QString,QDBusObjectPath)));
        }
        allConnected = jobRemovedSignalConnected && unitNewSignalConnected && unitRemovedSignalConnected;
        allConnected = connectUnitSignals(kControllerUnit) && allConnected;
        allConnected = connectUnitSignals(kWakewordUnit) && allConnected;
        if (!allConnected) {
            scheduleSystemdWatcherRetry();
        } else {
            systemdRetryTimer->stop();
        }
    }

    bool connectUnitSignals(const QString& unit) {
        const QString path = okay_hermes_realtime_tray::systemdUnitObjectPath(unit);
        if (unitSignalPaths.contains(path)) {
            return true;
        }
        const bool connected = QDBusConnection::sessionBus().connect(
            kSystemdService,
            path,
            kDBusPropertiesInterface,
            "PropertiesChanged",
            this,
            SLOT(systemdPropertiesChanged(QString,QVariantMap,QStringList)));
        if (connected) {
            unitSignalPaths.append(path);
        }
        return connected;
    }

    void setupStateWatchers() {
        const QString root = stateRootPath();
        QDir().mkpath(root);
        addWatch(captureWatcher, root, captureHealthPath());
        addWatch(controllerWatcher, root, controllerHealthPath());
    }

    static void addWatch(QFileSystemWatcher* watcher, const QString& directory, const QString& file) {
        if (!watcher->directories().contains(directory)) {
            watcher->addPath(directory);
        }
        if (QFileInfo(file).exists() && !watcher->files().contains(file)) {
            watcher->addPath(file);
        }
    }

    CaptureHealth captureHealth() const {
        return okay_hermes_realtime_tray::captureHealthFromStatusText(readStatusFile(captureHealthPath()));
    }

    ControllerHealth controllerHealth() const {
        return okay_hermes_realtime_tray::controllerHealthFromStatusText(readStatusFile(controllerHealthPath()));
    }

    bool microphoneAvailable() const {
        auto* context = PulseAudioQt::Context::instance();
        if (!context || context->state() != PulseAudioQt::Context::State::Ready || !context->server()) {
            return false;
        }
        auto* source = context->server()->defaultSource();
        if (!source) {
            return false;
        }
        const QVariantMap properties = source->pulseProperties();
        const QString mediaClass = properties.value(QStringLiteral("media.class")).toString();
        const QString deviceClass = properties.value(QStringLiteral("device.class")).toString();
        return mediaClass == QStringLiteral("Audio/Source") &&
               deviceClass != QStringLiteral("monitor") && !source->isVirtualDevice();
    }

    bool anyControlledUnitActive() const {
        return controllerActive || wakewordActive;
    }

    DaemonState daemonState() const {
        return okay_hermes_realtime_tray::stateFromInputs(
            microphoneAvailable(), captureHealth(), controllerActive, wakewordActive, controllerHealth());
    }

    void setupAudioWatcher() {
        auto* context = PulseAudioQt::Context::instance();
        QObject::connect(context, &PulseAudioQt::Context::stateChanged, this, [this] {
            reconnectAudioServerSignals();
            audioStateChanged();
        });
        QObject::connect(context, &PulseAudioQt::Context::sourceAdded, this,
                         [this](PulseAudioQt::Source*) { audioStateChanged(); });
        QObject::connect(context, &PulseAudioQt::Context::sourceRemoved, this,
                         [this](PulseAudioQt::Source*) { audioStateChanged(); });
        reconnectAudioServerSignals();
    }

    void reconnectAudioServerSignals() {
        auto* context = PulseAudioQt::Context::instance();
        auto* server = (context && context->state() == PulseAudioQt::Context::State::Ready)
                           ? context->server()
                           : nullptr;
        if (server == watchedAudioServer) {
            return;
        }
        QObject::disconnect(defaultSourceConnection);
        QObject::disconnect(serverUpdatedConnection);
        defaultSourceConnection = QMetaObject::Connection();
        serverUpdatedConnection = QMetaObject::Connection();
        watchedAudioServer = server;
        if (!watchedAudioServer) {
            return;
        }
        defaultSourceConnection = QObject::connect(
            watchedAudioServer, &PulseAudioQt::Server::defaultSourceChanged, this,
            [this](PulseAudioQt::Source*) { audioStateChanged(); });
        serverUpdatedConnection = QObject::connect(
            watchedAudioServer, &PulseAudioQt::Server::updated,
            this, &TrayController::audioStateChanged);
    }

    QApplication& app;
    std::unique_ptr<QSystemTrayIcon> tray;
    std::unique_ptr<QMenu> menu;
    QAction* turnOnAction = nullptr;
    QAction* turnOffAction = nullptr;
    QAction* openVoicePageAction = nullptr;
    QAction* exitAction = nullptr;
    QTimer* switchingTimer = nullptr;
    QTimer* systemdRetryTimer = nullptr;
    QFileSystemWatcher* captureWatcher = nullptr;
    QFileSystemWatcher* controllerWatcher = nullptr;
    QNetworkAccessManager* network = nullptr;
    QStringList unitSignalPaths;
    bool jobRemovedSignalConnected = false;
    bool unitNewSignalConnected = false;
    bool unitRemovedSignalConnected = false;
    bool controllerActive = false;
    bool wakewordActive = false;
    bool systemdCommandInFlight = false;
    bool openRequestInFlight = false;
    PulseAudioQt::Server* watchedAudioServer = nullptr;
    QMetaObject::Connection defaultSourceConnection;
    QMetaObject::Connection serverUpdatedConnection;
    int spinnerFrame = 0;
};

int main(int argc, char* argv[]) {
    QApplication app(argc, argv);
    QApplication::setApplicationName("Okay Hermes Realtime Tray");
    QApplication::setQuitOnLastWindowClosed(false);
    PulseAudioQt::Context::setApplicationId(QStringLiteral("okay-hermes-realtime-tray"));
    TrayController controller(app);
    return controller.run();
}

#include "main.moc"
