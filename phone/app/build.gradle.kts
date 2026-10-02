// Nour phone body, phase 0: notification forwarder + heartbeat (docs/adapters/phone.md §1).
// Dependencies are deliberately few: core-ktx, security-crypto (EncryptedSharedPreferences for
// the pairing token). JSON is org.json, HTTP is java.net.HttpURLConnection; both ship with Android.
plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "ae.nour.body"
    compileSdk = 35

    defaultConfig {
        applicationId = "ae.nour.body"
        minSdk = 31          // Android 12: FGS background-start rules and exact types we rely on
        targetSdk = 35       // Android 15: specialUse FGS from BOOT_COMPLETED, OTP redaction applies
        versionCode = 1
        versionName = "0.1.0"
    }

    buildFeatures {
        buildConfig = true   // BuildConfig.VERSION_NAME goes into the heartbeat
    }

    buildTypes {
        release {
            // No shrinking in phase 0: a sideloaded, reviewable APK; nothing to hide, nothing to break.
            isMinifyEnabled = false
            isShrinkResources = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.15.0")
    // EncryptedSharedPreferences. Upstream has deprecated this artifact; the planned replacement
    // (phone.md §1.4) is a non-exportable Keystore EC key used for mTLS / per-connection JWT,
    // which removes the stored token entirely. Fine for phase 0 on a device-owner-managed phone.
    implementation("androidx.security:security-crypto:1.1.0-alpha06")
}
