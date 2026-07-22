#pragma once

#include <QChar>
#include <QLatin1Char>
#include <QString>
#include <QStringView>

namespace okay_hermes_realtime_tray {

enum class DaemonState { Off, Starting, On, ConversationActive, NoMicrophone, Error };
enum class CaptureHealth { Unknown, Healthy, Unhealthy };
enum class ControllerHealth { Unknown, Ready, ConversationActive, Error };

inline CaptureHealth captureHealthFromStatusText(QStringView text) {
    const QString status = text.toString().trimmed();
    if (status == QStringLiteral("healthy")) {
        return CaptureHealth::Healthy;
    }
    if (status == QStringLiteral("unhealthy")) {
        return CaptureHealth::Unhealthy;
    }
    return CaptureHealth::Unknown;
}

inline ControllerHealth controllerHealthFromStatusText(QStringView text) {
    const QString status = text.toString().trimmed();
    if (status == QStringLiteral("ready")) {
        return ControllerHealth::Ready;
    }
    if (status == QStringLiteral("conversation-active")) {
        return ControllerHealth::ConversationActive;
    }
    if (status == QStringLiteral("error")) {
        return ControllerHealth::Error;
    }
    return ControllerHealth::Unknown;
}

inline QString systemdUnitObjectPath(QStringView unit) {
    QString escaped;
    escaped.reserve(unit.size() * 3);
    for (const QChar ch : unit) {
        if (ch.isLetterOrNumber()) {
            escaped.append(ch);
        } else {
            escaped.append(QStringLiteral("_%1").arg(
                static_cast<uint>(ch.unicode()), 2, 16, QLatin1Char('0')));
        }
    }
    return QStringLiteral("/org/freedesktop/systemd1/unit/") + escaped;
}

inline DaemonState stateFromInputs(
    bool microphoneAvailable,
    CaptureHealth captureHealth,
    bool controllerActive,
    bool wakewordActive,
    ControllerHealth controllerHealth) {
    if (!microphoneAvailable) {
        return DaemonState::NoMicrophone;
    }
    if ((wakewordActive && captureHealth == CaptureHealth::Unhealthy) ||
        controllerHealth == ControllerHealth::Error) {
        return DaemonState::Error;
    }
    const bool coherentRuntimeMode =
        (controllerActive && wakewordActive) || (!controllerActive && !wakewordActive);
    if (coherentRuntimeMode && captureHealth == CaptureHealth::Healthy &&
        controllerHealth == ControllerHealth::ConversationActive) {
        return DaemonState::ConversationActive;
    }
    if (coherentRuntimeMode && captureHealth == CaptureHealth::Healthy &&
        controllerHealth == ControllerHealth::Ready) {
        return DaemonState::On;
    }
    if (!controllerActive && !wakewordActive) {
        return DaemonState::Off;
    }
    return DaemonState::Starting;
}

}  // namespace okay_hermes_realtime_tray
