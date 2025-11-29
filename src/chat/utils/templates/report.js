let i, tab_content, tab_links;
tab_content = document.getElementsByClassName("tab-content");
tab_links = document.getElementsByClassName("tab-link");
if (tab_content.length > 0) tab_content[0].classList.add("active");
if (tab_links.length > 0) tab_links[0].classList.add("active");

// 跟踪哪些tab的图表已经初始化
const initializedTabs = new Set();

function showTab(evt, tabName) {
    for (i = 0; i < tab_content.length; i++) {
        tab_content[i].classList.remove("active");
        tab_content[i].style.animation = '';
    }
    for (i = 0; i < tab_links.length; i++) {
        tab_links[i].classList.remove("active");
    }
    document.getElementById(tabName).classList.add("active");
    document.getElementById(tabName).style.animation = 'slideIn 0.5s ease-out';
    evt.currentTarget.classList.add("active");
    
    // 懒加载：只在第一次切换到tab时初始化该tab的图表
    if (!initializedTabs.has(tabName) && tabName !== 'charts') {
        initializeStaticChartsForPeriod(tabName);
        initializedTabs.add(tabName);
    }
}

document.addEventListener('DOMContentLoaded', function () {
 // Chart data is injected by python via the HTML template.
    let allChartData = {};
    try {
        allChartData = JSON.parse(all_chart_data_json_string);
    } catch (e) {
        console.error("Failed to parse all_chart_data:", e);
        console.error("Problematic all_chart_data string:", all_chart_data_json_string);
    }

    let currentCharts = {};
    const chartConfigs = {
        totalCost: { id: 'totalCostChart', title: '总花费趋势', yAxisLabel: '花费 (¥)', dataKey: 'total_cost_data', fill: true },
        costByModule: { id: 'costByModuleChart', title: '各模块花费对比', yAxisLabel: '花费 (¥)', dataKey: 'cost_by_module', fill: false },
        costByModel: { id: 'costByModelChart', title: '各模型花费对比', yAxisLabel: '花费 (¥)', dataKey: 'cost_by_model', fill: false },
        messageByChat: { id: 'messageByChatChart', title: '各聊天流消息统计', yAxisLabel: '消息数', dataKey: 'message_by_chat', fill: false }
    };

    window.switchTimeRange = function(timeRange) {
        document.querySelectorAll('.time-range-btn').forEach(btn => btn.classList.remove('active'));
        event.target.classList.add('active');
        updateAllCharts(allChartData[timeRange], timeRange);
    }

    function updateAllCharts(data, timeRange) {
        Object.values(currentCharts).forEach(chart => chart && chart.destroy());
        currentCharts = {};
        Object.keys(chartConfigs).forEach(type => createChart(type, data, timeRange));
    }

    function createChart(chartType, data, timeRange) {
        const config = chartConfigs[chartType];
        if (!data || !data[config.dataKey]) return;
        // Material Design 3 Blue/Gray Color Palette
        const colors = ['#1976D2', '#546E7A', '#42A5F5', '#90CAF9', '#78909C', '#B0BEC5', '#1565C0', '#607D8B', '#2196F3', '#CFD8DC'];
        let datasets = [];
        if (chartType === 'totalCost') {
            datasets = [{ 
                label: config.title, 
                data: data[config.dataKey], 
                borderColor: '#1976D2', 
                backgroundColor: 'rgba(25, 118, 210, 0.1)', 
                tension: 0.3, 
                fill: config.fill,
                borderWidth: 2,
                pointRadius: 3,
                pointHoverRadius: 5,
                pointBackgroundColor: '#1976D2',
                pointBorderColor: '#fff',
                pointBorderWidth: 2
            }];
        } else {
            let i = 0;
            Object.entries(data[config.dataKey]).forEach(([name, chartData]) => {
                datasets.push({ 
                    label: name, 
                    data: chartData, 
                    borderColor: colors[i % colors.length], 
                    backgroundColor: colors[i % colors.length] + '30', 
                    tension: 0.4, 
                    fill: config.fill,
                    borderWidth: 2,
                    pointRadius: 3,
                    pointHoverRadius: 5,
                    pointBackgroundColor: colors[i % colors.length],
                    pointBorderColor: '#fff',
                    pointBorderWidth: 1
                });
                i++;
            });
        }
        const canvas = document.getElementById(config.id);
        if (!canvas) return;
        
        currentCharts[chartType] = new Chart(canvas, {
            type: 'line',
            data: { labels: data.time_labels, datasets: datasets },
            options: {
                responsive: true,
                maintainAspectRatio: true,
                aspectRatio: 2.5,
                plugins: { 
                    title: { 
                        display: true, 
                        text: `${config.title}`, 
                        font: { size: 16, weight: '500' },
                        color: '#1C1B1F',
                        padding: { top: 8, bottom: 16 }
                    }, 
                    legend: { 
                        display: chartType !== 'totalCost', 
                        position: 'top',
                        labels: {
                            usePointStyle: true,
                            padding: 15,
                            font: { size: 12 }
                        }
                    },
                    tooltip: {
                        backgroundColor: 'rgba(0, 0, 0, 0.8)',
                        padding: 12,
                        titleFont: { size: 14 },
                        bodyFont: { size: 13 },
                        cornerRadius: 8
                    }
                },
                scales: { 
                    x: { 
                        title: { 
                            display: true, 
                            text: '⏰ 时间',
                            font: { size: 13, weight: 'bold' }
                        },
                        ticks: { maxTicksLimit: 12 },
                        grid: { color: 'rgba(0, 0, 0, 0.05)' }
                    }, 
                    y: { 
                        title: { 
                            display: true, 
                            text: config.yAxisLabel,
                            font: { size: 13, weight: 'bold' }
                        },
                        beginAtZero: true,
                        grid: { color: 'rgba(0, 0, 0, 0.05)' }
                    } 
                },
                interaction: { 
                    intersect: false, 
                    mode: 'index' 
                },
                animation: {
                    duration: 1000,
                    easing: 'easeInOutQuart'
                }
            }
        });
    }

    if (allChartData['24h']) {
        updateAllCharts(allChartData['24h'], '24h');
        // Activate the 24h button by default
        document.querySelectorAll('.time-range-btn').forEach(btn => {
            if (btn.textContent.includes('24小时')) {
                btn.classList.add('active');
            } else {
                btn.classList.remove('active');
            }
        });
    }

    // Static charts
    let staticChartData = {};
    try {
        staticChartData = JSON.parse(static_chart_data_json_string);
    } catch (e) {
        console.error("Failed to parse static_chart_data:", e);
        console.error("Problematic static_chart_data string:", static_chart_data_json_string);
    }

    // 懒加载函数：只初始化指定tab的静态图表
    function initializeStaticChartsForPeriod(period_id) {
        if (!staticChartData[period_id]) {
            console.warn(`No static chart data for period: ${period_id}`);
            return;
        }
        const providerCostData = staticChartData[period_id].provider_cost_data;
        const moduleCostData = staticChartData[period_id].module_cost_data;
        const modelCostData = staticChartData[period_id].model_cost_data;
        // 扩展的Material Design调色板 - 包含多种蓝色系和其他配色
        const colors = [
            '#1976D2', '#42A5F5', '#2196F3', '#64B5F6', '#90CAF9', '#BBDEFB',  // 蓝色系
            '#1565C0', '#0D47A1', '#82B1FF', '#448AFF',  // 深蓝色系
            '#00BCD4', '#26C6DA', '#4DD0E1', '#80DEEA',  // 青色系
            '#009688', '#26A69A', '#4DB6AC', '#80CBC4',  // 青绿色系
            '#4CAF50', '#66BB6A', '#81C784', '#A5D6A7',  // 绿色系
            '#FF9800', '#FFA726', '#FFB74D', '#FFCC80',  // 橙色系
            '#FF5722', '#FF7043', '#FF8A65', '#FFAB91',  // 深橙色系
            '#9C27B0', '#AB47BC', '#BA68C8', '#CE93D8',  // 紫色系
            '#E91E63', '#EC407A', '#F06292', '#F48FB1',  // 粉色系
            '#607D8B', '#78909C', '#90A4AE', '#B0BEC5'   // 蓝灰色系
        ];

        // Provider Cost Pie Chart
        const providerCtx = document.getElementById(`providerCostPieChart_${period_id}`);
        if (providerCtx && providerCostData && providerCostData.data && providerCostData.data.length > 0) {
            new Chart(providerCtx, {
                type: 'doughnut',
                data: {
                    labels: providerCostData.labels,
                    datasets: [{
                        label: '按供应商花费',
                        data: providerCostData.data,
                        backgroundColor: colors,
                        borderColor: '#FFFFFF',
                        borderWidth: 2,
                        hoverOffset: 8
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        title: { 
                            display: false
                        },
                        legend: { 
                            position: 'right',
                            align: 'center',
                            labels: {
                                usePointStyle: true,
                                padding: 8,
                                font: { size: 10 },
                                boxWidth: 12,
                                boxHeight: 12,
                                color: function(context) {
                                    const chart = context.chart;
                                    const meta = chart.getDatasetMeta(0);
                                    const index = context.index;
                                    return meta.data[index] && meta.data[index].hidden ? '#CCCCCC' : '#666666';
                                },
                                generateLabels: function(chart) {
                                    const data = chart.data;
                                    if (data.labels.length && data.datasets.length) {
                                        const dataset = data.datasets[0];
                                        const labels = data.labels.slice(0, 10);
                                        return labels.map((label, i) => {
                                            const meta = chart.getDatasetMeta(0);
                                            const style = meta.controller.getStyle(i);
                                            const isHidden = meta.data[i] && meta.data[i].hidden;
                                            return {
                                                text: label.length > 15 ? label.substring(0, 15) + '...' : label,
                                                fillStyle: isHidden ? '#E0E0E0' : style.backgroundColor,
                                                strokeStyle: isHidden ? '#E0E0E0' : style.borderColor,
                                                lineWidth: style.borderWidth,
                                                fontColor: isHidden ? '#CCCCCC' : '#666666',
                                                hidden: isNaN(dataset.data[i]) || isHidden,
                                                index: i
                                            };
                                        });
                                    }
                                    return [];
                                }
                            },
                            onClick: function(e, legendItem, legend) {
                                const index = legendItem.index;
                                const chart = legend.chart;
                                const meta = chart.getDatasetMeta(0);
                                
                                // 切换该扇区的可见性
                                meta.data[index].hidden = !meta.data[index].hidden;
                                chart.update();
                            }
                        },
                        tooltip: {
                            backgroundColor: 'rgba(0, 0, 0, 0.8)',
                            padding: 12,
                            titleFont: { size: 13 },
                            bodyFont: { size: 12 },
                            cornerRadius: 8,
                            callbacks: {
                                label: function(context) {
                                    let label = context.label || '';
                                    if (label) {
                                        label += ': ';
                                    }
                                    label += context.parsed.toFixed(4) + ' ¥';
                                    const total = context.dataset.data.reduce((a, b) => a + b, 0);
                                    const percentage = ((context.parsed / total) * 100).toFixed(2);
                                    label += ` (${percentage}%)`;
                                    return label;
                                }
                            }
                        }
                    },
                    animation: {
                        animateRotate: true,
                        animateScale: true,
                        duration: 1000,
                        easing: 'easeInOutQuart'
                    }
                }
            });
        }

        // Module Cost Pie Chart
        const moduleCtx = document.getElementById(`moduleCostPieChart_${period_id}`);
        if (moduleCtx && moduleCostData && moduleCostData.data && moduleCostData.data.length > 0) {
            new Chart(moduleCtx, {
                type: 'doughnut',
                data: {
                    labels: moduleCostData.labels,
                    datasets: [{
                        label: '按模块花费',
                        data: moduleCostData.data,
                        backgroundColor: colors,
                        borderColor: '#FFFFFF',
                        borderWidth: 2,
                        hoverOffset: 8
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        title: { 
                            display: false
                        },
                        legend: { 
                            position: 'right',
                            align: 'center',
                            labels: {
                                usePointStyle: true,
                                padding: 8,
                                font: { size: 10 },
                                boxWidth: 12,
                                boxHeight: 12,
                                color: function(context) {
                                    const chart = context.chart;
                                    const meta = chart.getDatasetMeta(0);
                                    const index = context.index;
                                    return meta.data[index] && meta.data[index].hidden ? '#CCCCCC' : '#666666';
                                },
                                generateLabels: function(chart) {
                                    const data = chart.data;
                                    if (data.labels.length && data.datasets.length) {
                                        const dataset = data.datasets[0];
                                        return data.labels.map((label, i) => {
                                            const meta = chart.getDatasetMeta(0);
                                            const style = meta.controller.getStyle(i);
                                            const isHidden = meta.data[i] && meta.data[i].hidden;
                                            return {
                                                text: label.length > 15 ? label.substring(0, 15) + '...' : label,
                                                fillStyle: isHidden ? '#E0E0E0' : style.backgroundColor,
                                                strokeStyle: isHidden ? '#E0E0E0' : style.borderColor,
                                                lineWidth: style.borderWidth,
                                                fontColor: isHidden ? '#CCCCCC' : '#666666',
                                                hidden: isNaN(dataset.data[i]) || isHidden,
                                                index: i
                                            };
                                        });
                                    }
                                    return [];
                                }
                            },
                            onClick: function(e, legendItem, legend) {
                                const index = legendItem.index;
                                const chart = legend.chart;
                                const meta = chart.getDatasetMeta(0);
                                
                                meta.data[index].hidden = !meta.data[index].hidden;
                                chart.update();
                            }
                        },
                        tooltip: {
                            backgroundColor: 'rgba(0, 0, 0, 0.8)',
                            padding: 12,
                            titleFont: { size: 13 },
                            bodyFont: { size: 12 },
                            cornerRadius: 8,
                            callbacks: {
                                label: function(context) {
                                    let label = context.label || '';
                                    if (label) {
                                        label += ': ';
                                    }
                                    label += context.parsed.toFixed(4) + ' ¥';
                                    const total = context.dataset.data.reduce((a, b) => a + b, 0);
                                    const percentage = ((context.parsed / total) * 100).toFixed(2);
                                    label += ` (${percentage}%)`;
                                    return label;
                                }
                            }
                        }
                    },
                    animation: {
                        animateRotate: true,
                        animateScale: true,
                        duration: 1000,
                        easing: 'easeInOutQuart'
                    }
                }
            });
        }

        // Model Cost Bar Chart
        const modelCtx = document.getElementById(`modelCostBarChart_${period_id}`);
        if (modelCtx && modelCostData && modelCostData.data && modelCostData.data.length > 0) {
            // 动态计算高度：每个条目至少25px
            const minHeight = Math.max(250, modelCostData.labels.length * 25);
            modelCtx.parentElement.style.minHeight = minHeight + 'px';
            
            // 为每个柱子创建单独的数据集以支持单独隐藏
            const datasets = modelCostData.labels.map((label, idx) => ({
                label: label,
                data: modelCostData.labels.map((_, i) => i === idx ? modelCostData.data[idx] : null),
                backgroundColor: colors[idx % colors.length],
                borderColor: colors[idx % colors.length],
                borderWidth: 2,
                borderRadius: 6,
                hoverBackgroundColor: colors[idx % colors.length] + 'dd'
            }));
            
            new Chart(modelCtx, {
                type: 'bar',
                data: {
                    labels: modelCostData.labels,
                    datasets: datasets
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        title: { 
                            display: false
                        },
                        legend: { 
                            display: true,
                            position: 'top',
                            align: 'start',
                            labels: {
                                usePointStyle: true,
                                padding: 6,
                                font: { size: 9 },
                                boxWidth: 10,
                                boxHeight: 10
                            },
                            onClick: function(e, legendItem, legend) {
                                const index = legendItem.datasetIndex;
                                const chart = legend.chart;
                                const meta = chart.getDatasetMeta(index);
                                meta.hidden = meta.hidden === null ? !chart.data.datasets[index].hidden : null;
                                chart.update();
                            }
                        },
                        tooltip: {
                            backgroundColor: 'rgba(0, 0, 0, 0.8)',
                            padding: 12,
                            titleFont: { size: 13 },
                            bodyFont: { size: 12 },
                            cornerRadius: 8,
                            callbacks: {
                                label: function(context) {
                                    if (context.parsed.y !== null) {
                                        return context.dataset.label + ': ' + context.parsed.y.toFixed(4) + ' ¥';
                                    }
                                    return '';
                                }
                            },
                            filter: function(tooltipItem) {
                                return tooltipItem.parsed.y !== null;
                            }
                        }
                    },
                    scales: {
                        x: {
                            stacked: false,
                            grid: { display: false },
                            ticks: {
                                font: { size: 9 },
                                maxRotation: 45,
                                minRotation: 0,
                                callback: function(value, index, ticks) {
                                    const chart = this.chart;
                                    // 检查该索引位置是否有可见的数据
                                    let hasVisibleData = false;
                                    for (let i = 0; i < chart.data.datasets.length; i++) {
                                        const meta = chart.getDatasetMeta(i);
                                        if (!meta.hidden && chart.data.datasets[i].data[index] !== null) {
                                            hasVisibleData = true;
                                            break;
                                        }
                                    }
                                    // 只显示有可见数据的标签
                                    return hasVisibleData ? chart.data.labels[index] : '';
                                }
                            }
                        },
                        y: { 
                            beginAtZero: true, 
                            title: { 
                                display: true, 
                                text: '💰 花费 (¥)',
                                font: { size: 11, weight: 'bold' }
                            },
                            grid: { color: 'rgba(0, 0, 0, 0.05)' },
                            ticks: {
                                font: { size: 10 }
                            }
                        }
                    },
                    animation: {
                        duration: 1000,
                        easing: 'easeInOutQuart'
                    }
                }
            });
        }

        // === 新增图表 ===
        
        // 1. Token使用对比条形图
        const tokenCompData = staticChartData[period_id].token_comparison_data;
        const tokenCompCtx = document.getElementById(`tokenComparisonChart_${period_id}`);
        if (tokenCompCtx && tokenCompData && tokenCompData.labels && tokenCompData.labels.length > 0) {
            // 动态计算高度
            const minHeight = Math.max(270, tokenCompData.labels.length * 30);
            tokenCompCtx.parentElement.style.minHeight = minHeight + 'px';
            
            new Chart(tokenCompCtx, {
                type: 'bar',
                data: {
                    labels: tokenCompData.labels,
                    datasets: [
                        {
                            label: '输入Token',
                            data: tokenCompData.input_tokens,
                            backgroundColor: '#FF9800',
                            borderColor: '#F57C00',
                            borderWidth: 2,
                            borderRadius: 6
                        },
                        {
                            label: '输出Token',
                            data: tokenCompData.output_tokens,
                            backgroundColor: '#4CAF50',
                            borderColor: '#388E3C',
                            borderWidth: 2,
                            borderRadius: 6
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        title: { 
                            display: false
                        },
                        legend: { 
                            position: 'top',
                            labels: {
                                usePointStyle: true,
                                padding: 10,
                                font: { size: 11 },
                                boxWidth: 12,
                                boxHeight: 12
                            },
                            onClick: function(e, legendItem, legend) {
                                const index = legendItem.datasetIndex;
                                const chart = legend.chart;
                                const meta = chart.getDatasetMeta(index);
                                meta.hidden = meta.hidden === null ? !chart.data.datasets[index].hidden : null;
                                chart.update();
                            }
                        },
                        tooltip: {
                            backgroundColor: 'rgba(0, 0, 0, 0.8)',
                            padding: 12,
                            cornerRadius: 8,
                            callbacks: {
                                label: function(context) {
                                    const value = context.parsed.y.toLocaleString();
                                    const total = context.dataset.data.reduce((a, b) => a + b, 0);
                                    const percentage = ((context.parsed.y / total) * 100).toFixed(1);
                                    return context.dataset.label + ': ' + value + ' tokens (' + percentage + '%)';
                                }
                            }
                        }
                    },
                    scales: {
                        x: {
                            grid: { display: false },
                            ticks: { 
                                font: { size: 9 },
                                maxRotation: 45,
                                minRotation: 0
                            }
                        },
                        y: { 
                            beginAtZero: true,
                            title: { 
                                display: true, 
                                text: 'Token数量',
                                font: { size: 11, weight: 'bold' }
                            },
                            grid: { color: 'rgba(0, 0, 0, 0.05)' },
                            ticks: { font: { size: 10 } }
                        }
                    },
                    animation: { duration: 1000, easing: 'easeInOutQuart' },
                    interaction: {
                        mode: 'index',
                        intersect: false
                    }
                }
            });
        }

        // 2. 供应商请求占比环形图
        const providerReqData = staticChartData[period_id].provider_requests_data;
        const providerReqCtx = document.getElementById(`providerRequestsDoughnutChart_${period_id}`);
        if (providerReqCtx && providerReqData && providerReqData.data && providerReqData.data.length > 0) {
            new Chart(providerReqCtx, {
                type: 'doughnut',
                data: {
                    labels: providerReqData.labels,
                    datasets: [{
                        label: '请求数',
                        data: providerReqData.data,
                        backgroundColor: ['#9C27B0', '#E91E63', '#F44336', '#FF9800', '#FFC107', '#FFEB3B', '#CDDC39', '#8BC34A', '#4CAF50', '#009688'],
                        borderColor: '#FFFFFF',
                        borderWidth: 2,
                        hoverOffset: 10
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        title: { 
                            display: false
                        },
                        legend: { 
                            position: 'right',
                            align: 'center',
                            labels: {
                                usePointStyle: true,
                                padding: 8,
                                font: { size: 10 },
                                boxWidth: 12,
                                boxHeight: 12,
                                generateLabels: function(chart) {
                                    const data = chart.data;
                                    if (data.labels.length && data.datasets.length) {
                                        const dataset = data.datasets[0];
                                        return data.labels.map((label, i) => {
                                            const meta = chart.getDatasetMeta(0);
                                            const style = meta.controller.getStyle(i);
                                            return {
                                                text: label.length > 15 ? label.substring(0, 15) + '...' : label,
                                                fillStyle: style.backgroundColor,
                                                strokeStyle: style.borderColor,
                                                lineWidth: style.borderWidth,
                                                hidden: isNaN(dataset.data[i]) || meta.data[i].hidden,
                                                index: i
                                            };
                                        });
                                    }
                                    return [];
                                }
                            },
                            onClick: function(e, legendItem, legend) {
                                const index = legendItem.index;
                                const chart = legend.chart;
                                const meta = chart.getDatasetMeta(0);
                                
                                meta.data[index].hidden = !meta.data[index].hidden;
                                chart.update();
                            }
                        },
                        tooltip: {
                            backgroundColor: 'rgba(0, 0, 0, 0.8)',
                            padding: 12,
                            cornerRadius: 8,
                            callbacks: {
                                label: function(context) {
                                    const total = context.dataset.data.reduce((a, b) => a + b, 0);
                                    const percentage = ((context.parsed / total) * 100).toFixed(2);
                                    return context.label + ': ' + context.parsed + ' 次 (' + percentage + '%)';
                                }
                            }
                        }
                    },
                    animation: { animateRotate: true, animateScale: true, duration: 1000 }
                }
            });
        }

        // 3. 平均响应时间条形图
        const avgRespTimeData = staticChartData[period_id].avg_response_time_data;
        const avgRespTimeCtx = document.getElementById(`avgResponseTimeChart_${period_id}`);
        if (avgRespTimeCtx && avgRespTimeData && avgRespTimeData.data && avgRespTimeData.data.length > 0) {
            // 动态计算高度：横向条形图每个条目至少30px
            const minHeight = Math.max(270, avgRespTimeData.labels.length * 30);
            avgRespTimeCtx.parentElement.style.minHeight = minHeight + 'px';
            
            // 为每个柱子创建渐变色
            const barColors = avgRespTimeData.labels.map((_, idx) => {
                const colorPalette = ['#E91E63', '#9C27B0', '#673AB7', '#3F51B5', '#2196F3', '#00BCD4', '#009688', '#4CAF50'];
                return colorPalette[idx % colorPalette.length];
            });
            
            // 为每个柱子创建单独的数据集
            const datasets = avgRespTimeData.labels.map((label, idx) => ({
                label: label.length > 25 ? label.substring(0, 25) + '...' : label,
                data: avgRespTimeData.labels.map((_, i) => i === idx ? avgRespTimeData.data[idx] : null),
                backgroundColor: barColors[idx],
                borderColor: barColors[idx],
                borderWidth: 2,
                borderRadius: 6
            }));
            
            new Chart(avgRespTimeCtx, {
                type: 'bar',
                data: {
                    labels: avgRespTimeData.labels.map(label => label.length > 25 ? label.substring(0, 25) + '...' : label),
                    datasets: datasets
                },
                options: {
                    indexAxis: 'y',
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        title: { 
                            display: false
                        },
                        legend: { 
                            display: true,
                            position: 'top',
                            align: 'start',
                            labels: {
                                usePointStyle: true,
                                padding: 6,
                                font: { size: 9 },
                                boxWidth: 10,
                                boxHeight: 10
                            },
                            onClick: function(e, legendItem, legend) {
                                const index = legendItem.datasetIndex;
                                const chart = legend.chart;
                                const meta = chart.getDatasetMeta(index);
                                meta.hidden = meta.hidden === null ? !chart.data.datasets[index].hidden : null;
                                chart.update();
                            }
                        },
                        tooltip: {
                            backgroundColor: 'rgba(0, 0, 0, 0.8)',
                            padding: 12,
                            cornerRadius: 8,
                            callbacks: {
                                label: function(context) {
                                    if (context.parsed.x !== null) {
                                        return context.dataset.label + ': ' + context.parsed.x.toFixed(3) + ' 秒';
                                    }
                                    return '';
                                }
                            },
                            filter: function(tooltipItem) {
                                return tooltipItem.parsed.x !== null;
                            }
                        }
                    },
                    scales: {
                        x: { 
                            beginAtZero: true,
                            title: { 
                                display: true, 
                                text: '时间 (秒)',
                                font: { size: 11, weight: 'bold' }
                            },
                            grid: { color: 'rgba(0, 0, 0, 0.05)' },
                            ticks: { font: { size: 10 } }
                        },
                        y: {
                            grid: { display: false },
                            ticks: { font: { size: 9 } }
                        }
                    },
                    animation: { duration: 1000, easing: 'easeInOutQuart' }
                }
            });
        }

        // 4. 模型效率雷达图
        const radarData = staticChartData[period_id].model_efficiency_radar_data;
        const radarCtx = document.getElementById(`modelEfficiencyRadarChart_${period_id}`);
        if (radarCtx && radarData && radarData.datasets && radarData.datasets.length > 0) {
            const radarColors = ['#00BCD4', '#4CAF50', '#FF9800', '#E91E63', '#9C27B0'];
            const datasets = radarData.datasets.map((dataset, idx) => ({
                label: dataset.model.length > 20 ? dataset.model.substring(0, 20) + '...' : dataset.model,
                data: dataset.metrics,
                backgroundColor: radarColors[idx % radarColors.length] + '40',
                borderColor: radarColors[idx % radarColors.length],
                borderWidth: 2,
                pointBackgroundColor: radarColors[idx % radarColors.length],
                pointBorderColor: '#fff',
                pointHoverBackgroundColor: '#fff',
                pointHoverBorderColor: radarColors[idx % radarColors.length],
                pointRadius: 4,
                pointHoverRadius: 6
            }));
            
            new Chart(radarCtx, {
                type: 'radar',
                data: {
                    labels: radarData.labels,
                    datasets: datasets
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        title: { 
                            display: false
                        },
                        legend: { 
                            position: 'bottom',
                            labels: {
                                usePointStyle: true,
                                padding: 8,
                                font: { size: 10 },
                                boxWidth: 12,
                                boxHeight: 12
                            },
                            onClick: function(e, legendItem, legend) {
                                const index = legendItem.datasetIndex;
                                const chart = legend.chart;
                                const meta = chart.getDatasetMeta(index);
                                meta.hidden = meta.hidden === null ? !chart.data.datasets[index].hidden : null;
                                chart.update();
                            }
                        },
                        tooltip: {
                            backgroundColor: 'rgba(0, 0, 0, 0.8)',
                            padding: 12,
                            cornerRadius: 8,
                            callbacks: {
                                label: function(context) {
                                    const label = context.dataset.label || '';
                                    const metric = context.label || '';
                                    const value = context.parsed.r.toFixed(1);
                                    return label + ' - ' + metric + ': ' + value + '/100';
                                }
                            }
                        }
                    },
                    scales: {
                        r: {
                            beginAtZero: true,
                            max: 100,
                            ticks: {
                                stepSize: 20,
                                font: { size: 9 },
                                backdropColor: 'transparent'
                            },
                            pointLabels: {
                                font: { size: 10, weight: 'bold' }
                            },
                            grid: { color: 'rgba(0, 0, 0, 0.1)' },
                            angleLines: { color: 'rgba(0, 0, 0, 0.1)' }
                        }
                    },
                    animation: { duration: 1200, easing: 'easeInOutQuart' },
                    interaction: {
                        mode: 'point',
                        intersect: true
                    }
                }
            });
        }

        // 5. 响应时间分布散点图
        const scatterData = staticChartData[period_id].response_time_scatter_data;
        const scatterCtx = document.getElementById(`responseTimeScatterChart_${period_id}`);
        if (scatterCtx && scatterData && scatterData.length > 0) {
            // 按模型分组数据，限制每个模型最多显示100个点
            const groupedData = {};
            scatterData.forEach(point => {
                if (!groupedData[point.model]) {
                    groupedData[point.model] = [];
                }
                if (groupedData[point.model].length < 100) {
                    groupedData[point.model].push({x: point.x, y: point.y});
                }
            });
            
            const scatterColors = ['#4CAF50', '#2196F3', '#FF9800', '#E91E63', '#9C27B0', '#00BCD4', '#FFC107', '#607D8B'];
            const datasets = Object.keys(groupedData).slice(0, 8).map((model, idx) => ({
                label: model.length > 20 ? model.substring(0, 20) + '...' : model,
                data: groupedData[model],
                backgroundColor: scatterColors[idx % scatterColors.length] + '80',
                borderColor: scatterColors[idx % scatterColors.length],
                borderWidth: 2,
                pointRadius: 4,
                pointHoverRadius: 6,
                pointStyle: 'circle'
            }));
            
            new Chart(scatterCtx, {
                type: 'scatter',
                data: { datasets: datasets },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        title: { 
                            display: false
                        },
                        legend: { 
                            position: 'top',
                            labels: {
                                usePointStyle: true,
                                padding: 8,
                                font: { size: 10 },
                                boxWidth: 12,
                                boxHeight: 12
                            },
                            onClick: function(e, legendItem, legend) {
                                // 默认行为：切换数据集的可见性
                                const index = legendItem.datasetIndex;
                                const chart = legend.chart;
                                const meta = chart.getDatasetMeta(index);
                                
                                // 切换可见性
                                meta.hidden = meta.hidden === null ? !chart.data.datasets[index].hidden : null;
                                
                                // 更新图表
                                chart.update();
                            }
                        },
                        tooltip: {
                            backgroundColor: 'rgba(0, 0, 0, 0.8)',
                            padding: 12,
                            cornerRadius: 8,
                            callbacks: {
                                label: function(context) {
                                    return context.dataset.label + ': ' + context.parsed.y.toFixed(3) + ' 秒';
                                },
                                afterLabel: function(context) {
                                    return '请求 #' + context.parsed.x;
                                }
                            }
                        }
                    },
                    scales: {
                        x: {
                            title: { 
                                display: true, 
                                text: '请求序号',
                                font: { size: 11, weight: 'bold' }
                            },
                            grid: { color: 'rgba(0, 0, 0, 0.05)' },
                            ticks: { font: { size: 10 } }
                        },
                        y: { 
                            beginAtZero: true,
                            title: { 
                                display: true, 
                                text: '响应时间 (秒)',
                                font: { size: 11, weight: 'bold' }
                            },
                            grid: { color: 'rgba(0, 0, 0, 0.05)' },
                            ticks: { font: { size: 10 } }
                        }
                    },
                    animation: { duration: 1000, easing: 'easeInOutQuart' },
                    interaction: {
                        mode: 'point',
                        intersect: true
                    }
                }
            });
        }
    }
    
    // 初始化第一个tab(默认显示的tab)的图表
    const firstTab = tab_content[0]?.id;
    if (firstTab && firstTab !== 'charts') {
        initializeStaticChartsForPeriod(firstTab);
        initializedTabs.add(firstTab);
    }
});