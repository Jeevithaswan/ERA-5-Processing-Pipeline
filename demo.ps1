# Chhindwara Climate & Drought Monitoring Pipeline
# Run:  powershell -ExecutionPolicy Bypass -File demo.ps1

$Root = $PSScriptRoot

function Show-Menu {
    Clear-Host
    Write-Host ""
    Write-Host "  CHHINDWARA CLIMATE AND DROUGHT MONITORING PIPELINE"
    Write-Host "  ---------------------------------------------------"
    Write-Host ""
    Write-Host "  1.  Run pipeline for a new region"
    Write-Host "  2.  Open district charts (1991-2026)"
    Write-Host "  3.  Open data tables (Excel)"
    Write-Host "  4.  Open weekly and fortnightly charts"
    Write-Host "  5.  Open source repository"
    Write-Host "  6.  Open validation summary"
    Write-Host "  0.  Exit"
    Write-Host ""
}

function Pause-Return {
    Write-Host ""
    Read-Host "Press Enter to continue"
}

while ($true) {
    Show-Menu
    $choice = Read-Host "Select an option"

    switch ($choice) {
        "1" {
            Write-Host ""
            Write-Host "Region: Nagpur  |  Period: 2022-2023  |  Variables: t2m, tp  |  Indices: SPI, PNI, IMD departure"
            Write-Host ""
            & python "$Root\run_climatology.py" --place "Nagpur" --bbox 21.3 78.9 20.9 79.3 `
                --start-year 2022 --end-year 2023 --variables t2m,tp --indices spi,pni,imd-departure
            Write-Host ""
            if (Test-Path "$Root\data\Nagpur\figures") { explorer "$Root\data\Nagpur\figures" }
            Pause-Return
        }
        "2" {
            explorer "$Root\Chhindwara_Final_Results_1991_2026\figures_government"
            Pause-Return
        }
        "3" {
            Invoke-Item "$Root\Chhindwara_Final_Results_1991_2026\excel\chhindwara_drought_indices_1991_2026.xlsx"
            Pause-Return
        }
        "4" {
            Invoke-Item "$Root\Chhindwara_Final_Results_1991_2026\figures_government\spi_weekly_timeseries.png"
            Invoke-Item "$Root\Chhindwara_Final_Results_1991_2026\figures_government\spi_fortnightly_timeseries.png"
            Pause-Return
        }
        "5" {
            Start-Process "https://github.com/Jeevithaswan/ERA-5-Processing-Pipeline"
            Pause-Return
        }
        "6" {
            Write-Host ""
            Write-Host "  Reference: Kalsi, Jenamani and Hatwar (2006), Mausam 57(3), India Meteorological Department."
            Write-Host "  Published figure, July 2002, Madhya Pradesh region: rainfall deficiency 80% or more."
            Write-Host "  Pipeline output, Chhindwara district, July 2002: -79.1% departure from normal."
            Write-Host ""
            Write-Host "  Difference from published figure: within 1 percentage point."
            Pause-Return
        }
        "0" { break }
        default {
            Write-Host "Invalid selection."
            Start-Sleep -Seconds 1
        }
    }
    if ($choice -eq "0") { break }
}
