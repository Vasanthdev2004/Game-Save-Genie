; Inno Setup script for the Game Save Genie Windows installer.
; Requires Inno Setup 6.3 or newer (for the x64compatible architecture id).
; Build (after packaging\build_exe.ps1 has produced dist\gsg.exe):
;   ISCC packaging\installer.iss /DMyAppVersion=0.5.0
; Produces dist\GameSaveGenie-Setup.exe — per-user install, no admin needed.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#if Ver < EncodeVer(6, 3, 0)
  #error Game Save Genie's installer requires Inno Setup 6.3 or newer.
#endif

[Setup]
AppId={{5707FE2C-F3D3-4563-A7AF-7CE605D6DF5F}
AppName=Game Save Genie
AppVersion={#MyAppVersion}
AppPublisher=Vasanthdev2004
AppPublisherURL=https://github.com/Vasanthdev2004/Game-Save-Genie
AppSupportURL=https://github.com/Vasanthdev2004/Game-Save-Genie/issues
DefaultDirName={localappdata}\Programs\Game Save Genie
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=GameSaveGenie-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ChangesEnvironment=yes
UninstallDisplayName=Game Save Genie
WizardStyle=modern

[Files]
Source: "..\dist\gsg.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; Open a terminal in the install dir so `gsg` is ready to type — a bare
; console-exe shortcut would just flash help and close once configured.
Name: "{userprograms}\Game Save Genie"; Filename: "{cmd}"; Parameters: "/k ""{app}\gsg.exe"""; WorkingDir: "{app}"

[Registry]
; Append the install dir to the user PATH so `gsg` works in any terminal.
Root: HKCU; Subkey: "Environment"; ValueName: "Path"; ValueType: expandsz; ValueData: "{olddata};{app}"; Check: NeedsAddPath(ExpandConstant('{app}'))

[Run]
; /k keeps the window open regardless of config state (wizard on first run,
; help + prompt once configured) instead of flashing and closing.
Filename: "{cmd}"; Parameters: "/k ""{app}\gsg.exe"""; Description: "Get started with Game Save Genie"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Remove the start-at-boot entry the app may have installed.
Filename: "{app}\gsg.exe"; Parameters: "auto --uninstall"; Flags: runhidden skipifdoesntexist; RunOnceId: "RemoveAutostart"

[Code]
function NeedsAddPath(Param: string): boolean;
var
  OrigPath: string;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', OrigPath) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Uppercase(Param) + ';', ';' + Uppercase(OrigPath) + ';') = 0;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Path, AppDir: string;
  P: Integer;
begin
  if CurUninstallStep <> usUninstall then
    exit;
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', Path) then
    exit;
  AppDir := ExpandConstant('{app}');
  { Strip our segment however it was joined: ';dir', 'dir;', or bare 'dir'. }
  P := Pos(Uppercase(';' + AppDir), Uppercase(Path));
  if P > 0 then
    Delete(Path, P, Length(AppDir) + 1)
  else
  begin
    P := Pos(Uppercase(AppDir + ';'), Uppercase(Path));
    if P > 0 then
      Delete(Path, P, Length(AppDir) + 1)
    else
    begin
      P := Pos(Uppercase(AppDir), Uppercase(Path));
      if P > 0 then
        Delete(Path, P, Length(AppDir));
    end;
  end;
  RegWriteExpandStringValue(HKCU, 'Environment', 'Path', Path);
end;
